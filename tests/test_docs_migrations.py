"""The Schema Management docs say what the code does (#539).

The promises the pages make, each checked against the implementation:

- every ``ferro migrate`` output a page quotes (a ```text block marked
  ``<!-- output: <id> -->``) is what the verb prints with no terminal, on a
  scratch SQLite project, once the parts that vary are normalised;
- every step file a page quotes (a block headed ``-- NNNN_<name>/<file>``, or a
  Python block headed ``# migrations/NNNN_<name>/<file>``) is, byte for byte,
  what ``ferro migrate new`` writes from the page's example models;
- every Alembic revision the bridge page quotes (``<!-- revision: <id> -->``)
  is what ``alembic revision --autogenerate`` writes for that change;

- the two declaration styles of every migrations example generate the same
  migration, byte for byte (the ``_annotated`` twin's ``migrations/`` equals
  the assignment twin's);
- every example of the renames and retirements page starts from the one
  running example the page declares;
- the migrations reference lists every ``PassReport`` statement role and every
  warning kind the core raises, in the core's order;
- every refusal the CLI reference quotes is the text ``ferro`` prints, for
  the refusals that need no database;
- the Migrations API page documents exactly ``ferro.migrations.__all__``, and
  every identifier an API page documents resolves at runtime;
- no page says "in-house", "Alembic territory" or "door" (the strategies are named
  Auto-migrate, Migrations and Alembic).
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import ModuleType

import pytest

import ferro.migrations
from tests._docs_harness import Fence, fenced_blocks, page_fences, run_python

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_PAGES = REPO_ROOT / "docs" / "pages"
DOCS_EXAMPLES = REPO_ROOT / "docs" / "examples"
CLI_REFERENCE = DOCS_PAGES / "reference" / "cli.md"
API_PAGE = DOCS_PAGES / "api" / "migrations.md"

SHOWN_PROJECT = "/srv/app"
"""The project directory the CLI reference shows in a quoted refusal."""

MIGRATION_EXAMPLES = [
    "migrations_quickstart",
    "migrations_backfill",
    "migrations_rename",
    "migrations_retire",
    "migrations_retire_label",
    "migrations_squash",
    "migrations_testing",
]


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def _run_example(name: str, project: Path) -> None:
    run_python([str(DOCS_EXAMPLES / f"{name}.py"), str(project)])


@pytest.mark.parametrize("name", MIGRATION_EXAMPLES)
def test_both_declaration_styles_generate_the_same_migrations(
    name: str, tmp_path: Path
) -> None:
    assignment, annotated = tmp_path / "assignment", tmp_path / "annotated"
    _run_example(name, assignment)
    _run_example(f"{name}_annotated", annotated)

    written = _files(assignment / "migrations")
    assert any(path.endswith("ir.json") for path in written), written
    assert _files(annotated / "migrations") == written


RENAMES_PAGE_EXAMPLES = [
    "migrations_rename",
    "migrations_retire",
    "migrations_retire_label",
]
"""The examples of the renames and retirements page, each one change to its running
example, which ``migrations_rename``'s ``base`` region declares."""


def _examples_project() -> ModuleType:
    """``docs/examples/_migrations_project.py``, which reads an example's regions."""
    sys.path.insert(0, str(DOCS_EXAMPLES))
    try:
        return importlib.import_module("_migrations_project")
    finally:
        sys.path.remove(str(DOCS_EXAMPLES))


@pytest.mark.parametrize("style", ["", "_annotated"])
@pytest.mark.parametrize("name", RENAMES_PAGE_EXAMPLES)
def test_each_renames_page_example_starts_from_the_page_s_running_example(
    name: str, style: str
) -> None:
    project = _examples_project()
    change = project._region(DOCS_EXAMPLES / f"{name}{style}.py", "models")
    base = project._region(DOCS_EXAMPLES / f"migrations_rename{style}.py", "base")
    assert project._before(change) == base


# -- the refusals the CLI reference quotes -------------------------------------------

MODELS = """\
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
"""

REKEYED = """\
from ferro import Field, Model


class Author(Model):
    id: int | None = None
    name: str = Field(primary_key=True)
"""


def _ferro(
    project: Path, *args: str, scratch: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run ``ferro`` in ``project`` with a clean environment. ``scratch`` points
    ``DATABASE_URL`` at the project's own SQLite file, ``app.db``, or at the URL
    its ``.database_url`` file names (a Postgres scenario)."""
    env = {"PATH": "", "PYTHONDONTWRITEBYTECODE": "1"}
    if scratch:
        named = project / ".database_url"
        env["DATABASE_URL"] = (
            named.read_text()
            if named.exists()
            else f"sqlite:{project / 'app.db'}?mode=rwc"
        )
        env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from ferro.cli import main; sys.exit(main())",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=project,
        env=env,
    )


def _missing_dialects(project: Path) -> None:
    (project / "ferro.toml").write_text('models = ["blog.models"]\n')


def _both_config_files(project: Path) -> None:
    (project / "ferro.toml").write_text(
        'models = ["blog.models"]\ndialects = ["postgres"]\n'
    )
    (project / "pyproject.toml").write_text(
        '[tool.ferro]\nmodels = ["blog.models"]\ndialects = ["postgres"]\n'
    )


def _unknown_key(project: Path) -> None:
    (project / "ferro.toml").write_text(
        'models = ["blog.models"]\ndialects = ["postgres"]\nmigrations_dir = "db"\n'
    )


def _primary_key_change(project: Path) -> None:
    (project / "ferro.toml").write_text(
        'models = ["blog.models"]\ndialects = ["postgres", "sqlite"]\n'
    )
    (project / "blog").mkdir()
    (project / "blog" / "__init__.py").write_text("")
    (project / "blog" / "models.py").write_text(MODELS)
    created = _ferro(project, "migrate", "new", "create_author")
    assert created.returncode == 0, created.stderr
    (project / "blog" / "models.py").write_text(REKEYED)


def _empty_squash(project: Path) -> None:
    """``0001_create_author``, then ``0002_add_bio`` adds a column and
    ``0003_drop_bio`` drops it: squashed from ``0002``, nothing is left."""
    (project / "ferro.toml").write_text(
        'models = ["blog.models"]\ndialects = ["postgres", "sqlite"]\n'
    )
    (project / "blog").mkdir()
    (project / "blog" / "__init__.py").write_text("")
    models = project / "blog" / "models.py"
    for name, body in (
        ("create_author", MODELS),
        ("add_bio", MODELS + "    bio: str | None = None\n"),
        ("drop_bio", MODELS),
    ):
        models.write_text(body)
        made = _ferro(project, "migrate", "new", name)
        assert made.returncode == 0, made.stderr


REFUSALS = {
    "missing-dialects": (_missing_dialects, ("migrate", "check")),
    "both-config-files": (_both_config_files, ("migrate", "check")),
    "unknown-key": (_unknown_key, ("migrate", "check")),
    "primary-key-change": (_primary_key_change, ("migrate", "new", "rekey")),
    "squash-empty": (_empty_squash, ("migrate", "squash", "bio", "--from", "0002")),
}


def _marked(kind: str, blocks: list[Fence] | None = None) -> dict[str, str]:
    """Each ```text block marked ``<!-- <kind>: <id> -->``, by id.

    ``blocks`` defaults to every block a docs page shows, so a marked block
    on any page is compared: there is no page where the note goes unchecked.

    Raises:
        AssertionError: Two blocks carry one id.
    """
    found: dict[str, str] = {}
    for fence in page_fences("text") if blocks is None else blocks:
        if fence.note is None or fence.note[0] != kind:
            continue
        block_id = fence.note[1]
        assert block_id not in found, (
            f"{fence.where}: {kind} id {block_id!r} marks two blocks"
        )
        found[block_id] = fence.body
    return found


def _registry_gaps(
    kind: str, registry: dict, blocks: list[Fence] | None = None
) -> list[str]:
    """What keeps the marked blocks and ``registry`` from naming the same ids."""
    marked = _marked(kind, blocks)
    return [
        *(
            f"<!-- {kind}: {block_id} --> has no entry in the registry"
            for block_id in sorted(set(marked) - set(registry))
        ),
        *(
            f"the registry's {block_id!r} marks no block"
            for block_id in sorted(set(registry) - set(marked))
        ),
    ]


def _quoted_refusals() -> dict[str, str]:
    return _marked("refusal")


def test_the_pages_quote_each_database_free_refusal() -> None:
    assert _registry_gaps("refusal", REFUSALS) == []


@pytest.mark.parametrize("kind", ["output", "refusal"])
def test_a_marked_block_on_any_page_is_compared(kind: str) -> None:
    """A typed block under ``output:`` or ``refusal:`` on a page outside the
    migrations guides still has to name a registered scenario."""
    page = DOCS_PAGES / "howto" / "testing.md"
    fabricated = fenced_blocks(
        page,
        f"# Testing\n\n<!-- {kind}: made-up -->\n```text\nferro migrate: typed\n```\n",
        "text",
    )
    registry = OUTPUTS if kind == "output" else REFUSALS
    blocks = [*page_fences("text"), *fabricated]
    assert _registry_gaps(kind, registry, blocks) == [
        f"<!-- {kind}: made-up --> has no entry in the registry"
    ]


@pytest.mark.parametrize("refusal", sorted(REFUSALS))
def test_a_quoted_refusal_is_the_text_ferro_prints(
    refusal: str, tmp_path: Path
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    arrange, args = REFUSALS[refusal]
    arrange(project)

    result = _ferro(project, *args)

    assert result.returncode == 1, result.stderr
    printed = result.stderr.replace(str(project.resolve()), SHOWN_PROJECT)
    printed = printed.replace(str(project), SHOWN_PROJECT)
    assert printed == _quoted_refusals()[refusal]


# -- the API page --------------------------------------------------------------------


def test_the_api_page_documents_every_public_name_of_ferro_migrations() -> None:
    targets = re.findall(
        r"^::: (ferro\.migrations\.[\w.]+)", API_PAGE.read_text(), re.M
    )
    # Only `ferro.migrations.<name>`: the testing harness and the reports
    # documented under their defining module (`ferro.migrations.runner.RunReport`)
    # are not in `ferro.migrations.__all__`.
    documented = sorted(
        target.rsplit(".", 1)[1] for target in targets if target.count(".") == 2
    )
    assert documented == sorted(ferro.migrations.__all__)


def _api_targets() -> list[str]:
    """Every ``::: <identifier>`` on every API reference page."""
    return [
        target
        for page in sorted((DOCS_PAGES / "api").glob("*.md"))
        for target in re.findall(r"^::: ([\w.]+)$", page.read_text(), re.M)
    ]


def _resolve(identifier: str) -> object:
    """``identifier`` reached the way a reader's code reaches it: attribute by
    attribute from ``ferro``, importing a submodule only where the package
    has no attribute of that name (as ``import ferro.migrations.testing``
    does)."""
    first, *rest = identifier.split(".")
    found: object = importlib.import_module(first)
    for part in rest:
        if not hasattr(found, part) and isinstance(found, ModuleType):
            importlib.import_module(f"{found.__name__}.{part}")
        found = getattr(found, part)
    return found


@pytest.mark.parametrize("identifier", _api_targets())
def test_each_api_page_identifier_resolves_at_runtime(identifier: str) -> None:
    _resolve(identifier)


def test_the_api_pages_document_every_public_name_of_ferro() -> None:
    import ferro

    documented = {id(_resolve(target)) for target in _api_targets()}
    undocumented = [
        name for name in ferro.__all__ if id(getattr(ferro, name)) not in documented
    ]
    assert undocumented == []


def test_the_pass_report_types_and_settings_error_are_public_names() -> None:
    import ferro

    for name in ("PassReport", "ExecutedStatement", "Report", "SettingsError"):
        assert name in ferro.__all__, name
    assert ferro.Report is ferro.pass_report.Report


def test_baseline_and_drift_are_the_functions_on_the_package() -> None:
    from ferro.migrations._baseline import baseline
    from ferro.migrations._drift import drift

    assert ferro.migrations.baseline is baseline
    assert ferro.migrations.drift is drift


def test_the_api_page_documents_the_test_harness() -> None:
    page = API_PAGE.read_text()
    for name in ("harness", "Harness", "RoundTripResult"):
        assert f"::: ferro.migrations.testing.{name}\n" in page, name


# -- the PassReport the migrations reference describes ------------------------------

MIGRATIONS_REFERENCE = DOCS_PAGES / "reference" / "migrations.md"
REPORT_KINDS_SOURCE = REPO_ROOT / "crates" / "ferro-ddl-lowering" / "src" / "lib.rs"


def _first_column(page: Path, header: str) -> list[str]:
    """The backticked first cell of every row of the table whose header row
    starts with ``header``."""
    lines = page.read_text().splitlines()
    start = next(number for number, line in enumerate(lines) if line.startswith(header))
    cells = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells.append(line.split("|")[1].strip().strip("`").strip('"'))
    return cells


def test_the_reference_lists_every_report_kind_the_core_raises() -> None:
    source = REPORT_KINDS_SOURCE.read_text()
    body = source[source.index("pub enum ReportKind {") :]
    body = body[: body.index("\n}\n")]
    kinds = re.findall(r"^    ([A-Z]\w+)\b", body, flags=re.MULTILINE)
    assert kinds, "no ReportKind variant found"
    assert _first_column(MIGRATIONS_REFERENCE, "| `kind` |") == kinds


def test_the_reference_lists_every_executed_statement_role() -> None:
    from typing import get_args

    from ferro.pass_report import Role

    roles = _first_column(MIGRATIONS_REFERENCE, "| `role` |")
    assert roles == list(get_args(Role))


# -- the strategies' names ---------------------------------------------------------------


@pytest.mark.parametrize("phrase", ["in-house", "Alembic territory", "door"])
def test_no_page_names_a_strategy_the_old_way(phrase: str) -> None:
    hits = [
        f"{path.relative_to(DOCS_PAGES)}:{number}"
        for path in sorted(DOCS_PAGES.rglob("*.md"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if phrase in line
    ]
    assert hits == []


# -- the output the CLI reference and the guides quote -------------------------------
#
# A block marked ``<!-- output: <id> -->`` is what ``ferro migrate`` prints with no
# terminal (a pipe, as in a CI log): the plain form. Each id names a scenario on a
# scratch SQLite project and the verb run against it; the block must equal what the
# verb printed (stdout, then stderr) once the parts that differ from run to run are
# normalised the same way on both sides.

OUTPUT_PAGES = [
    CLI_REFERENCE,
    DOCS_PAGES / "guide" / "schema" / "migrations.md",
    DOCS_PAGES / "guide" / "schema" / "renames-and-retirements.md",
    DOCS_PAGES / "guide" / "schema" / "squash.md",
    DOCS_PAGES / "guide" / "schema" / "data-steps.md",
    DOCS_PAGES / "guide" / "schema" / "deploying.md",
    DOCS_PAGES / "guide" / "schema" / "adopting-migrations.md",
]

HEAD = "from ferro import Field, Model\n\n\n"

AUTHOR = (
    HEAD
    + """\
class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
"""
)
AUTHOR_NICKNAME = (
    AUTHOR + "    nickname: str | None = Field(default=None, index=True)\n"
)

NAMED_AUTHOR = (
    HEAD
    + """\
class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
"""
)
NAMED_AUTHOR_SLUG = NAMED_AUTHOR + "    slug: str\n"
NAMED_AUTHOR_OPTIONAL_SLUG = NAMED_AUTHOR + "    slug: str | None = None\n"

AUTHOR_NICKNAME_REQUIRED = AUTHOR + "    nickname: str = Field(index=True)\n"


def _write_models(project: Path, source: str) -> None:
    (project / "blog" / "models.py").write_text(source)


def _database(project: Path, sql: str) -> None:
    """Run ``sql`` on the project's SQLite database, the way an operator would."""
    import sqlite3

    connection = sqlite3.connect(project / "app.db")
    try:
        connection.executescript(sql)
        connection.commit()
    finally:
        connection.close()


def _run_ok(project: Path, *args: str) -> None:
    result = _ferro(project, *args, scratch=True)
    assert result.returncode == 0, (
        f"ferro {' '.join(args)}\n{result.stdout}{result.stderr}"
    )


def _configured(project: Path) -> None:
    (project / "ferro.toml").write_text(
        'models = ["blog.models"]\ndialects = ["postgres", "sqlite"]\n'
    )
    (project / "blog").mkdir(exist_ok=True)
    (project / "blog" / "__init__.py").write_text("")


def _unconfigured(project: Path) -> None:
    (project / "blog").mkdir()
    (project / "blog" / "__init__.py").write_text("")
    _write_models(project, AUTHOR)


# The migrations guide's loop: an Author with a new, indexed nickname.


def _initialised(project: Path) -> None:
    _unconfigured(project)
    _run_ok(
        project, "migrate", "init", "--models", "blog.models",
        "--dialects", "postgres,sqlite", "--config-file", "ferro.toml",
    )  # fmt: skip


def _loop_edited(project: Path) -> None:
    _configured(project)
    _write_models(project, AUTHOR)
    _run_ok(project, "migrate", "new", "create_author")
    _run_ok(project, "migrate", "up")
    _write_models(project, AUTHOR_NICKNAME)


def _loop_generated(project: Path) -> None:
    _loop_edited(project)
    _run_ok(project, "migrate", "new", "author_nickname")


def _loop_applied(project: Path) -> None:
    _loop_generated(project)
    _run_ok(project, "migrate", "up")


def _loop_drifted(project: Path) -> None:
    _loop_applied(project)
    _database(project, "DROP INDEX idx_author_nickname")


def _loop_edited_step(project: Path) -> None:
    """An applied step whose file was changed afterwards."""
    _loop_applied(project)
    step = project / "migrations" / "0001_create_author" / "01_schema.up.sqlite.sql"
    step.write_text(step.read_text() + "\n-- edited\n")


def _feature_branch(project: Path) -> None:
    """The CLI reference's squash: ``0002_add_bio`` with a filled-in
    hand-written SQL step ``fix_rows``, then ``0003_add_team``."""
    bio = AUTHOR + "    bio: str | None = None\n"
    team = (
        "\n\nclass Team(Model):\n"
        "    id: int | None = Field(default=None, primary_key=True)\n"
        "    label: str\n"
    )
    _configured(project)
    _write_models(project, AUTHOR)
    _run_ok(project, "migrate", "new", "create_author")
    _write_models(project, bio)
    _run_ok(project, "migrate", "new", "add_bio", "--sql-step", "fix_rows")
    step = project / "migrations" / "0002_add_bio" / "02_fix_rows.up.sql"
    step.write_text("UPDATE author SET bio = name;\n")
    _write_models(project, bio + team)
    _run_ok(project, "migrate", "new", "add_team")


def _created_and_applied(project: Path) -> None:
    _configured(project)
    _write_models(project, AUTHOR)
    _run_ok(project, "migrate", "new", "create_author")
    _run_ok(project, "migrate", "up")


def _sql_step_added(project: Path) -> None:
    _created_and_applied(project)
    _run_ok(project, "migrate", "new", "seed", "--sql-step", "seed_admins")


# The renames page: its running example, and nickname renamed with no hint.

RENAMES_BASE = (
    "from enum import StrEnum\n\n"
    + HEAD
    + """\
class Status(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELED = "canceled"


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    nickname: str | None = Field(default=None, index=True)
    bio: str = Field(index=True)
    status: Status = Status.ACTIVE
"""
)


def _before_rename(project: Path) -> None:
    _configured(project)
    _write_models(project, RENAMES_BASE)
    _run_ok(project, "migrate", "new", "create_author")
    _run_ok(project, "migrate", "up")
    _write_models(project, RENAMES_BASE.replace("nickname:", "handle:"))


def _rebuild_blocked(project: Path) -> None:
    """The loop's database, with an index and a trigger ferro never declared, meets
    a migration that makes ``nickname`` required: a SQLite rebuild."""
    _loop_applied(project)
    _database(
        project,
        "CREATE INDEX my_idx ON author(name);"
        " CREATE TRIGGER t AFTER INSERT ON author BEGIN SELECT 1; END;",
    )
    _write_models(project, AUTHOR_NICKNAME_REQUIRED)
    _run_ok(
        project,
        "migrate",
        "new",
        "nickname_required",
        "--no-backfill",
        "author.nickname",
    )


# Two branches that both generated migration 0002: a teammate's, the loop's
# ``0002_author_nickname``, and yours, ``0002_author_bio``.

AUTHOR_BIO = AUTHOR + "    bio: str | None = None\n"
AUTHOR_NICKNAME_AND_BIO = AUTHOR_NICKNAME + "    bio: str | None = None\n"


def _two_branches(project: Path, *, applied: bool) -> None:
    import shutil

    _configured(project)
    _write_models(project, AUTHOR)
    _run_ok(project, "migrate", "new", "create_author")
    _run_ok(project, "migrate", "up")
    _write_models(project, AUTHOR_NICKNAME)
    _run_ok(project, "migrate", "new", "author_nickname")
    teammate = project / "migrations" / "0002_author_nickname"
    shutil.move(teammate, project / "teammate")
    _write_models(project, AUTHOR_BIO)
    _run_ok(project, "migrate", "new", "author_bio", "--sql-step", "fix_rows")
    if applied:
        _run_ok(project, "migrate", "up")
    shutil.move(project / "teammate", teammate)
    _write_models(project, AUTHOR_NICKNAME_AND_BIO)


def _two_branches_unapplied(project: Path) -> None:
    _two_branches(project, applied=False)


def _two_branches_replaced(project: Path) -> None:
    _two_branches(project, applied=True)
    _run_ok(project, "migrate", "new", "author_bio", "--replace", "0002_author_bio")


# A feature branch, one migration per task, squashed before it merges.

TEAM = (
    "\n\nclass Team(Model):\n"
    "    id: int | None = Field(default=None, primary_key=True)\n"
    "    name: str\n"
)
TEAM_TMP_CODE = "    tmp_code: str | None = None\n"
TEAM_SLUG = "    slug: str\n"


def _teams_branch(project: Path) -> None:
    """Main holds ``0001_create_author``, the loop's ``Author``; the branch's tasks
    add ``0002_add_teams`` (with a ``tmp_code`` column), ``0003_team_slug`` (a
    required slug and its written backfill), ``0004_drop_tmp`` and
    ``0005_fix_rows`` (a hand-written SQL step). The developer's database applied
    every one."""
    _configured(project)
    models = AUTHOR_NICKNAME
    _write_models(project, models)
    _run_ok(project, "migrate", "new", "create_author")
    _write_models(project, models + TEAM + TEAM_TMP_CODE)
    _run_ok(project, "migrate", "new", "add_teams")
    _write_models(project, models + TEAM + TEAM_TMP_CODE + TEAM_SLUG)
    _run_ok(project, "migrate", "new", "team_slug")
    backfill = project / "migrations" / "0003_team_slug" / "02_backfill_team.py"
    backfill.write_text(
        backfill.read_text().replace(
            'todo("the slug for an existing team")', "team.name.lower()"
        )
    )
    _write_models(project, models + TEAM + TEAM_SLUG)
    _run_ok(project, "migrate", "new", "drop_tmp")
    _run_ok(project, "migrate", "new", "fix_rows", "--sql-step", "fix_rows")
    fix_rows = project / "migrations" / "0005_fix_rows"
    (fix_rows / "01_fix_rows.up.sql").write_text("UPDATE team SET name = trim(name);\n")
    (fix_rows / "01_fix_rows.down.sql").write_text(
        "-- ferro: nothing-to-reverse the names stay trimmed\n"
    )
    _run_ok(project, "migrate", "up")


def _teams_branch_squashed(project: Path) -> None:
    _teams_branch(project)
    _run_ok(project, "migrate", "squash", "teams", "--from", "0002")


def _teams_branch_dead_name(project: Path) -> None:
    """``0003_team_slug``'s backfill, a chunked step, selects its rows by
    ``tmp_code``, a column that exists only inside the range."""
    _teams_branch(project)
    backfill = project / "migrations" / "0003_team_slug" / "02_backfill_team.py"
    backfill.write_text(
        backfill.read_text().replace("team.slug == None", "team.tmp_code == None")
    )


def _teams_branch_partly_applied(project: Path) -> None:
    """A teammate's database: it applied ``0002``..``0004`` and not ``0005``."""
    _teams_branch(project)
    _run_ok(project, "migrate", "down", "--to", "0004", "--yes")
    _run_ok(project, "migrate", "squash", "teams", "--from", "0002")


# A required column, a backfill, and the rows the backfill is for.


def _slug_edited(project: Path) -> None:
    _configured(project)
    _write_models(project, NAMED_AUTHOR)
    _run_ok(project, "migrate", "new", "create_author")
    _run_ok(project, "migrate", "up")
    _database(project, "INSERT INTO author (name) VALUES ('Ada Lovelace')")
    _write_models(project, NAMED_AUTHOR_SLUG)


def _slug_unwritten(project: Path) -> None:
    _slug_edited(project)
    _run_ok(project, "migrate", "new", "author_slug")


TODO = 'todo("the slug for an existing author")'
WRITTEN = 'author.name.lower().replace(" ", "-")'
NOT_APPLICABLE = "-- ferro: not-applicable\n"
LATE_ROW = "-- ferro: expand\nINSERT INTO author (name) VALUES ('Late');\n"


def _slug_written(project: Path) -> None:
    _slug_unwritten(project)
    backfill = project / "migrations" / "0002_author_slug" / "02_backfill_author.py"
    backfill.write_text(backfill.read_text().replace(TODO, WRITTEN))


def _late_row_step(project: Path) -> Path:
    return (
        project / "migrations" / "0002_author_slug" / "03_add_constraint.up.sqlite.sql"
    )


def _slug_late_row(project: Path) -> None:
    """A writer that does not know the column yet inserts a row after the backfill."""
    _slug_written(project)
    step = _late_row_step(project)
    step.write_text(LATE_ROW)


def _slug_late_row_failed(project: Path) -> None:
    _slug_late_row(project)
    assert _ferro(project, "migrate", "up", scratch=True).returncode == 1


def _slug_late_row_reverted(project: Path) -> None:
    _slug_late_row_failed(project)
    _run_ok(project, "migrate", "down", "--to", "0002:01", "--yes")
    step = _late_row_step(project)
    step.write_text(NOT_APPLICABLE)


# The Deploying guide's two-release chain, as docs/examples/migration_errors.py
# builds it: 0002_author_slug_nullable (one expand step), then
# 0003_author_slug_required (backfill, add-constraint, contract), so the page
# names one migration per number.

REQUIRED = "0003_author_slug_required"


def _chain_written(project: Path) -> None:
    _slug_edited(project)
    _write_models(project, NAMED_AUTHOR_OPTIONAL_SLUG)
    _run_ok(project, "migrate", "new", "author_slug_nullable")
    _write_models(project, NAMED_AUTHOR_SLUG)
    _run_ok(project, "migrate", "new", "author_slug_required")
    backfill = project / "migrations" / REQUIRED / "01_backfill_author.py"
    backfill.write_text(backfill.read_text().replace(TODO, WRITTEN))


def _chain_late_row_failed(project: Path) -> None:
    """A writer inserts a row without a slug after the backfill, so the
    contract step fails."""
    _chain_written(project)
    step = project / "migrations" / REQUIRED / "02_add_constraint.up.sqlite.sql"
    step.write_text(LATE_ROW)
    assert _ferro(project, "migrate", "up", scratch=True).returncode == 1


# A database built by auto-migrate, about to be baselined.


def _adopt_base(project: Path) -> None:
    _configured(project)
    _write_models(project, NAMED_AUTHOR)
    _run_ok(project, "migrate", "new", "create_author")
    first = project / "migrations" / "0001_create_author" / "01_schema.up.sqlite.sql"
    _database(project, first.read_text())
    _write_models(project, NAMED_AUTHOR_OPTIONAL_SLUG)
    _run_ok(project, "migrate", "new", "author_slug")


def _adopt_initial(project: Path) -> None:
    """Models described by ``0001_create_author``; the database was built by the
    old strategy."""
    _initialised(project)
    _run_ok(project, "migrate", "new", "create_author")
    first = project / "migrations" / "0001_create_author" / "01_schema.up.sqlite.sql"
    _database(project, first.read_text())


def _adopt_ahead(project: Path) -> None:
    """The database already has the ``slug`` that migration 0002 adds."""
    _adopt_base(project)
    _database(project, "ALTER TABLE author ADD COLUMN slug varchar")


STAGING = ("--url", "sqlite:staging.db")
"""A second database beside ``app.db``: the staging copy the adopting how-to
baselines with ``--url "$STAGING_URL"``."""


def _adopt_staging(project: Path) -> None:
    """A staging copy restored from a dump taken at ``0001``."""
    import shutil

    _adopt_base(project)
    shutil.copy(project / "app.db", project / "staging.db")


def _adopt_staged(project: Path) -> None:
    _adopt_staging(project)
    _run_ok(project, "migrate", "baseline", "0001", *STAGING)


def _adopt_baselined(project: Path) -> None:
    _adopt_ahead(project)
    _run_ok(project, "migrate", "baseline")


def _adopt_foreign_default(project: Path) -> None:
    _configured(project)
    _write_models(project, NAMED_AUTHOR_SLUG)
    _run_ok(project, "migrate", "new", "create_author")
    _database(
        project,
        "CREATE TABLE author (id integer NOT NULL PRIMARY KEY AUTOINCREMENT,"
        " name varchar NOT NULL, slug varchar NOT NULL DEFAULT (lower('unknown')));"
        " CREATE INDEX idx_author_name ON author(name);",
    )


# The waits a second run shows: it needs a holder that outlives the setup.


def _lock_wait(project: Path) -> None:
    _loop_generated(project)


@contextmanager
def _run_lock_held(project: Path) -> Iterator[None]:
    """Another run holds the SQLite run lock, an OS lock on ``<database>.ferro-migrate.lock``."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        pytest.skip("the SQLite run lock is held here with fcntl")
    with open(project / "app.db.ferro-migrate.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _postgres_base() -> str:
    from tests.db_backends import get_postgres_url

    base = get_postgres_url(dict(os.environ), REPO_ROOT / ".env")
    if not base:
        pytest.skip("FERRO_POSTGRES_URL is not set: this output needs Postgres")
    return base


def _ddl_lock_retry(project: Path) -> None:
    """Postgres: ``author`` applied, and a column to add to it, in a schema of its own."""
    import uuid

    import psycopg

    from tests.db_backends import build_postgres_test_url

    base = _postgres_base()
    schema = f"ferro_docs_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    (project / ".schema").write_text(schema)
    (project / ".database_url").write_text(build_postgres_test_url(base, schema))
    _configured(project)
    (project / "ferro.toml").write_text(
        'models = ["blog.models"]\ndialects = ["postgres"]\nddl_lock_timeout = "1s"\n'
    )
    _write_models(project, NAMED_AUTHOR)
    _run_ok(project, "migrate", "new", "create_author")
    _run_ok(project, "migrate", "up")
    _write_models(project, NAMED_AUTHOR_OPTIONAL_SLUG)
    _run_ok(project, "migrate", "new", "author_slug_nullable")


@contextmanager
def _author_table_locked(project: Path) -> Iterator[None]:
    """Another session holds ``author`` until the step has waited twice."""
    from types import SimpleNamespace

    from tests.test_ddl_lock_timeout import Holder

    base = _postgres_base()
    schema = (project / ".schema").read_text()
    with Holder(SimpleNamespace(base=base, schema=schema), "author", seconds=3.5):
        yield


def _drop_postgres_schemas(templates: list[Path]) -> None:
    import psycopg

    for template in templates:
        marker = template / ".schema"
        if marker.exists():
            with psycopg.connect(_postgres_base(), autocommit=True) as admin:
                admin.execute(f'DROP SCHEMA IF EXISTS "{marker.read_text()}" CASCADE')


HELD_WHILE_RUNNING = {
    "run-lock-wait": _run_lock_held,
    "ddl-lock-retry": _author_table_locked,
}
"""The outputs that only exist while something else holds a lock: the context a
test enters around the verb."""


OUTPUTS = {
    # the CLI reference
    "init": (
        _unconfigured,
        (
            "migrate",
            "init",
            "--models",
            "blog.models",
            "--dialects",
            "postgres,sqlite",
            "--config-file",
            "ferro.toml",
        ),
    ),
    "init-pyproject": (
        _unconfigured,
        (
            "migrate",
            "init",
            "--models",
            "blog.models",
            "--dialects",
            "postgres,sqlite",
            "--config-file",
            "pyproject.toml",
        ),
    ),
    "new-small": (_loop_edited, ("migrate", "new", "author_nickname")),
    "squash": (_feature_branch, ("migrate", "squash", "teams", "--from", "0002")),
    "check-ok": (_loop_generated, ("migrate", "check")),
    "check-problem": (_loop_edited, ("migrate", "check")),
    "up": (_loop_generated, ("migrate", "up")),
    "down": (_loop_applied, ("migrate", "down", "--yes")),
    "status-pending": (_loop_generated, ("migrate", "status", "--steps")),
    "status-applied": (_loop_applied, ("migrate", "status")),
    "status-attention": (_loop_edited_step, ("migrate", "status")),
    "drift-clean": (_loop_applied, ("migrate", "drift")),
    "drift-differences": (_loop_drifted, ("migrate", "drift")),
    "baseline": (_adopt_ahead, ("migrate", "baseline")),
    "rerecord": (_loop_edited_step, ("migrate", "rerecord", "0001:01")),
    # the migrations guide
    "loop-check": (_loop_edited, ("migrate", "check")),
    "loop-new": (_loop_edited, ("migrate", "new", "author_nickname")),
    "loop-up": (_loop_generated, ("migrate", "up")),
    "sql-step": (
        _created_and_applied,
        ("migrate", "new", "seed", "--sql-step", "seed_admins"),
    ),
    "data-step": (
        _sql_step_added,
        ("migrate", "new", "fix_slugs", "--data-only", "--data-step", "Author"),
    ),
    "rebuild-refused": (_rebuild_blocked, ("migrate", "up")),
    "checking-drift": (_loop_drifted, ("migrate", "drift")),
    "going-back": (_loop_applied, ("migrate", "down", "--yes")),
    "duplicate-number": (_two_branches_unapplied, ("migrate", "check")),
    "replace": (
        _two_branches_unapplied,
        ("migrate", "new", "author_bio", "--replace", "0002_author_bio"),
    ),
    "replaced-up-refused": (_two_branches_replaced, ("migrate", "up")),
    "rekey-refused": (_primary_key_change, ("migrate", "new", "rekey")),
    # renames and retirements
    "rename-hint": (_before_rename, ("migrate", "new", "rename_handle")),
    # squashing
    "squash-teams": (_teams_branch, ("migrate", "squash", "teams", "--from", "0002")),
    "squash-dead-name": (
        _teams_branch_dead_name,
        ("migrate", "squash", "teams", "--from", "0002"),
    ),
    "squash-status-inheritable": (_teams_branch_squashed, ("migrate", "status")),
    "squash-up-inherits": (_teams_branch_squashed, ("migrate", "up")),
    "squash-partial-witness": (_teams_branch_partly_applied, ("migrate", "up")),
    # data steps
    "backfill-new": (_slug_edited, ("migrate", "new", "author_slug")),
    "backfill-unwritten": (_slug_unwritten, ("migrate", "up")),
    "backfill-up": (_slug_written, ("migrate", "up")),
    "late-row-up": (_slug_late_row, ("migrate", "up")),
    "late-row-down": (
        _slug_late_row_failed,
        ("migrate", "down", "--to", "0002:01", "--yes"),
    ),
    "late-row-resume": (_slug_late_row_reverted, ("migrate", "up")),
    # deploying
    "run-lock-wait": (_lock_wait, ("migrate", "up", "--lock-timeout", "1s")),
    "ddl-lock-retry": (_ddl_lock_retry, ("migrate", "up")),
    "ci-check": (_chain_written, ("migrate", "check")),
    "server-up": (_chain_written, ("migrate", "up")),
    "status-steps": (_chain_late_row_failed, ("migrate", "status", "--steps")),
    "edited-up": (_loop_edited_step, ("migrate", "up")),
    "edited-rerecord": (_loop_edited_step, ("migrate", "rerecord", "0001:01")),
    # adopting
    "adopt-new-initial": (_initialised, ("migrate", "new", "create_author")),
    "adopt-baseline": (_adopt_initial, ("migrate", "baseline")),
    "adopt-up-refused": (_adopt_base, ("migrate", "up")),
    "baseline-drift": (_adopt_ahead, ("migrate", "baseline", "0001")),
    "baseline-foreign-default": (_adopt_foreign_default, ("migrate", "baseline")),
    "staging-baseline": (_adopt_staging, ("migrate", "baseline", "0001", *STAGING)),
    "staging-up": (_adopt_staged, ("migrate", "up", *STAGING)),
    "status-baselined": (_adopt_baselined, ("migrate", "status")),
    "baseline-remove": (_adopt_baselined, ("migrate", "baseline", "--remove")),
    "down-baselined-refused": (_adopt_baselined, ("migrate", "down", "--yes")),
}


def _quoted_blocks(page: Path) -> list[tuple[str | None, str | None, str, str]]:
    """Every ```text block of ``page`` as (kind, id, first line, body), the kind and
    id from the ``<!-- output|refusal: <id> -->`` marker above it, if any."""
    blocks = []
    for fence in fenced_blocks(page, page.read_text(), "text"):
        kind, block_id = fence.note or (None, None)
        if kind not in ("output", "refusal"):
            kind = block_id = None
        blocks.append((kind, block_id, fence.body.split("\n", 1)[0], fence.body))
    return blocks


def _quoted_outputs() -> dict[str, str]:
    return _marked("output")


def _normalise(text: str) -> str:
    """The parts of ferro's output that differ from run to run, made equal."""
    text = re.sub(
        r"sqlite:(?://)?\S*?(\w+)\.db(?:\?mode=rwc)?", r"sqlite:///./\1.db", text
    )
    text = re.sub(
        r"postgres(?:ql)?://[^:\s/]+:\*+@\S+",
        "postgresql://app:***@localhost/app",
        text,
    )
    text = re.sub(r"\(\d+ ms\)", "(N ms)", text)
    text = re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\dZ", "YYYY-MM-DD HH:MMZ", text)
    text = re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC", "YYYY-MM-DD HH:MM UTC", text)
    return re.sub(r"sha384:[0-9a-f]+…?", "sha384:<digest>", text)


def test_every_output_block_has_a_registry_entry_and_the_reverse() -> None:
    assert _registry_gaps("output", OUTPUTS) == []


CLI_LINE = re.compile(
    r"^\s*(?:"
    r"\$ ferro |ferro migrate: |warning: |\S+ · "  # a command, a refusal, a warning, the database line
    r"|(?:ok|!!|xx|--|~~) \S"  # a plain-form marker
    r"|\d{4}_\w+ {2,}\S+ {2,}(?:applied|reverted|failed|waiting)"  # a step row
    r"|ok: models match|no drift against|drift against |nothing to (?:apply|revert)"
    r"|no schema change|recorded \S+ as baseline|re-recorded |removed the baseline"
    r"|(?:ungenerated|unwritten_step|duplicate_number|broken_chain): "
    r"|Another ferro migration run"
    r")"
)
"""A line that only ``ferro migrate`` prints, whatever its block opens with."""


def test_every_block_that_shows_ferro_output_is_marked() -> None:
    """A ```text block holding any line ``ferro migrate`` prints carries a marker."""
    unmarked = [
        f"{page.relative_to(DOCS_PAGES)}: {line.strip()}"
        for page in OUTPUT_PAGES
        for kind, _id, _first, body in _quoted_blocks(page)
        if kind is None
        for line in body.splitlines()
        if CLI_LINE.match(line)
    ]
    assert unmarked == []


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory: pytest.TempPathFactory):
    """Build each scenario once; hand every test its own copy of the project."""
    import shutil

    built: dict[Callable[[Path], None], Path] = {}

    def make(setup: Callable[[Path], None]) -> Path:
        if setup not in built:
            template = tmp_path_factory.mktemp(setup.__name__.strip("_")) / "app"
            template.mkdir()
            setup(template)
            built[setup] = template
        copy = tmp_path_factory.mktemp("run") / "app"
        shutil.copytree(built[setup], copy)
        return copy

    yield make
    _drop_postgres_schemas(list(built.values()))


@pytest.mark.parametrize("output", sorted(OUTPUTS))
def test_a_quoted_output_is_what_ferro_prints_without_a_terminal(
    output: str, scenarios
) -> None:
    setup, args = OUTPUTS[output]
    project = scenarios(setup)

    with HELD_WHILE_RUNNING.get(output, lambda _project: nullcontext())(project):
        result = _ferro(project, *args, scratch=True)

    printed = _normalise(result.stdout + result.stderr)
    assert printed == _normalise(_quoted_outputs()[output])


# -- the step files the guides quote --------------------------------------------------
#
# A fenced block whose first line is ``-- NNNN_<name>/<file>`` quotes a step file
# ``ferro migrate new`` wrote; a block may quote several, each under its own
# header line. A Python block whose first line is ``# migrations/NNNN_<name>/<file>``
# quotes one generated data step. Each quoted file must be, byte for byte, what the
# page's example (or a scenario of this module) writes when it runs: the step file is
# regenerated from the models the page shows, never kept by hand.

STEP_FILE_HEADER = re.compile(r"^-- (?P<migration>\d{4}_[a-z0-9_]+)/(?P<file>\S+)$")
DATA_STEP_HEADER = re.compile(
    r"^# migrations/(?P<migration>\d{4}_[a-z0-9_]+)/(?P<file>\S+\.py)$"
)


def _slug_guarded(project: Path) -> None:
    _slug_edited(project)
    _run_ok(project, "migrate", "new", "author_slug", "--no-backfill", "author.slug")


ORDER = (
    "from enum import StrEnum\n\n"
    + HEAD
    + """\
class Status(StrEnum):
    PAID = "paid"
    CANCELED = "canceled"


class Order(Model):
    id: int | None = Field(default=None, primary_key=True)
    status: Status = Status.PAID
"""
)


def _label_removed(project: Path) -> None:
    """``Status`` loses ``canceled``, a label rows may still hold."""
    _configured(project)
    _write_models(project, ORDER)
    _run_ok(project, "migrate", "new", "create_order")
    _write_models(project, ORDER.replace('    CANCELED = "canceled"\n', ""))
    _run_ok(project, "migrate", "new", "drop_canceled")


INVOICE = """\
import uuid
from typing import ClassVar

from ferro import Field, Model, RowPolicy, RowSecurity


class Invoice(Model):
    id: int | None = Field(default=None, primary_key=True)
    tenant_id: uuid.UUID
    customer: str
    total: int
"""
INVOICE_RLS = (
    INVOICE
    + """
    __ferro_rls__: ClassVar = RowSecurity(
        RowPolicy(column="tenant_id", setting="app.tenant_id")
    )
"""
)


def _invoice_rls(project: Path) -> None:
    """The row-level security guide's ``Invoice``, which exists, gains its policy."""
    _configured(project)
    _write_models(project, INVOICE)
    _run_ok(project, "migrate", "new", "create_invoice")
    _write_models(project, INVOICE_RLS)
    _run_ok(project, "migrate", "new", "invoice_rls")


STEP_FILE_SOURCES: dict[tuple[str, ...], str | Callable[[Path], None]] = {
    ("guide/schema/migrations.md", "0001_create_author"): "migrations_quickstart",
    ("guide/schema/migrations.md", "0002_author_nickname"): "migrations_quickstart",
    (
        "guide/schema/renames-and-retirements.md",
        "0002_rename_writer",
    ): "migrations_rename",
    (
        "guide/schema/renames-and-retirements.md",
        "0002_retire_bio",
    ): "migrations_retire",
    ("guide/schema/squash.md", "0002_teams"): "migrations_squash",
    ("reference/migrations.md", "0002_author_nickname"): "migrations_quickstart",
    ("guide/schema/data-steps.md", "0002_author_slug"): "migrations_backfill",
    (
        "guide/schema/data-steps.md",
        "0002_author_slug",
        "02_backfill_author.py",
    ): _slug_unwritten,
    (
        "guide/schema/data-steps.md",
        "0002_author_slug",
        "02_guard_author.py",
    ): _slug_guarded,
    ("guide/schema/data-steps.md", "0002_drop_canceled"): _label_removed,
    ("guide/row-level-security.md", "0002_invoice_rls"): _invoice_rls,
    (
        "guide/schema/migrations.md",
        "0002_add_writer",
        "03_backfill_writer.py",
    ): None,
}
"""What writes the migration a page quotes: an example's name, or a scenario. Keyed by
(page, migration), or by (page, migration, file) where one file of a migration comes
from elsewhere (an unwritten backfill, a guard step in place of it). ``None`` marks a
data step the page shows already written by hand, which nothing generates."""


def _step_files_in(body: str) -> list[tuple[str, str, str]]:
    """The step files one block quotes, as (migration, file, text): the text runs
    from under its header line to the next header or the block's end, blank lines
    around it dropped."""
    first, _, rest = body.partition("\n")
    data_step = DATA_STEP_HEADER.match(first)
    if data_step:
        return [(data_step["migration"], data_step["file"], rest)]
    if not STEP_FILE_HEADER.match(first):
        return []
    quoted = []
    header: re.Match[str] | None = None
    rows: list[str] = []
    for row in [*body.splitlines(), None]:
        match = None if row is None else STEP_FILE_HEADER.match(row)
        if row is not None and match is None:
            rows.append(row)
            continue
        if header is not None:
            text = "\n".join(rows).strip("\n") + "\n"
            quoted.append((header["migration"], header["file"], text))
        header, rows = match, []
    return quoted


def _quoted_step_files() -> list[tuple[str, int, str, str, str]]:
    """Every step file a page quotes, as (page, block line, migration, file, text)."""
    return [
        (page.relative_to(DOCS_PAGES).as_posix(), line, migration, file, text)
        for page in sorted(DOCS_PAGES.rglob("*.md"))
        for line, body in (
            (f.line, f.body) for f in fenced_blocks(page, page.read_text())
        )
        for migration, file, text in _step_files_in(body)
    ]


def _step_file_source(page: str, migration: str, file: str) -> tuple[str, ...] | None:
    for key in ((page, migration, file), (page, migration)):
        if key in STEP_FILE_SOURCES:
            return key
    return None


def test_every_quoted_step_file_has_a_source_and_the_reverse() -> None:
    used = {
        _step_file_source(page, migration, file)
        for page, _line, migration, file, _text in _quoted_step_files()
    }
    assert None not in used, "a quoted step file has no source in STEP_FILE_SOURCES"
    assert used == set(STEP_FILE_SOURCES)


@pytest.fixture(scope="module")
def example_projects(tmp_path_factory: pytest.TempPathFactory):
    """Run each example once, into a project it leaves behind."""
    built: dict[str, Path] = {}

    def make(name: str) -> Path:
        if name not in built:
            project = tmp_path_factory.mktemp(name) / "app"
            _run_example(name, project)
            built[name] = project
        return built[name]

    return make


def _step_file_id(value: object) -> str:
    """The page, block line, migration and file name a test is about; not the text."""
    if isinstance(value, int):
        return str(value)
    return value if isinstance(value, str) and "\n" not in value else ""


def _generated_step_files() -> list[tuple[str, int, str, str, str]]:
    """The quoted step files to regenerate: every one but those written by hand
    (one with no source stays, and fails naming itself)."""
    generated = []
    for page, line, migration, file, text in _quoted_step_files():
        key = _step_file_source(page, migration, file)
        if key is None or STEP_FILE_SOURCES[key] is not None:
            generated.append((page, line, migration, file, text))
    return generated


@pytest.mark.parametrize(
    ("page", "line", "migration", "file", "text"),
    _generated_step_files(),
    ids=_step_file_id,
)
def test_a_quoted_step_file_is_what_the_generator_writes(
    page: str,
    line: int,
    migration: str,
    file: str,
    text: str,
    example_projects,
    scenarios,
) -> None:
    where = f"{page}:{line} quotes {migration}/{file}"
    key = _step_file_source(page, migration, file)
    assert key, f"{where}, a migration STEP_FILE_SOURCES names no source for"
    source = STEP_FILE_SOURCES[key]
    if isinstance(source, str):
        project, name = example_projects(source), source
    else:
        project, name = scenarios(source), source.__name__
    written = project / "migrations" / migration / file
    assert written.is_file(), f"{where}, which {name} never writes"
    assert text == written.read_text(), f"{where}, which {name} writes otherwise"


# -- the Alembic revisions the bridge page quotes -----------------------------------
#
# A ```python block marked ``<!-- revision: <id> -->`` is the ``upgrade()`` and
# ``downgrade()`` that ``alembic revision --autogenerate`` writes for a change, on a
# SQLite database the before models built, without Alembic's ``# ###`` banner lines.

ALEMBIC_PAGE = DOCS_PAGES / "guide" / "schema" / "alembic.md"

REVISIONS = {
    "nickname": (AUTHOR, AUTHOR_NICKNAME),
    "drop-bio": (AUTHOR + "    bio: str | None = None\n", AUTHOR),
}
"""Each quoted revision's models: (before, after)."""

AUTOGENERATE = """\
import asyncio
import sys

import sqlalchemy as sa
from alembic.autogenerate import produce_migrations, render_python_code
from alembic.migration import MigrationContext

import ferro
import blog.models  # noqa: F401
from ferro.migrations import ferro_options, get_metadata


async def main(step):
    if step == "create":
        await ferro.connect("sqlite:app.db?mode=rwc", auto_migrate=True)
        return
    engine = sa.create_engine("sqlite:///app.db")
    options = ferro_options()
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True, **options},
        )
        script = produce_migrations(context, get_metadata())
    engine.dispose()
    for name, ops in (("upgrade", script.upgrade_ops), ("downgrade", script.downgrade_ops)):
        print(f"def {name}() -> None:")
        for line in render_python_code(ops, render_item=options["render_item"]).splitlines():
            if line.strip() and not line.strip().startswith("# ###"):
                print("    " + line.strip())
        if name == "upgrade":
            print("\\n")


asyncio.run(main(sys.argv[1]))
"""
"""Builds the database from the before models (``create``), then, run again over
the after models, prints the revision as the page quotes it."""


def _quoted_revisions() -> dict[str, str]:
    found = re.findall(
        r"<!-- revision: ([a-z0-9-]+) -->\n```python[^\n]*\n(.*?)```",
        ALEMBIC_PAGE.read_text(),
        flags=re.DOTALL,
    )
    return dict(found)


def test_every_quoted_revision_has_models_and_the_reverse() -> None:
    assert sorted(_quoted_revisions()) == sorted(REVISIONS)


@pytest.mark.parametrize("revision", sorted(REVISIONS))
def test_a_quoted_revision_is_what_autogenerate_writes(
    revision: str, tmp_path: Path
) -> None:
    pytest.importorskip("alembic", reason="the alembic extra is not installed")
    before, after = REVISIONS[revision]
    project = tmp_path / "app"
    (project / "blog").mkdir(parents=True)
    (project / "blog" / "__init__.py").write_text("")
    (project / "autogenerate.py").write_text(AUTOGENERATE)
    printed = ""
    for models, step in ((before, "create"), (after, "render")):
        _write_models(project, models)
        result = run_python(
            ["autogenerate.py", step],
            timeout=120,
            cwd=project,
            env={"PYTHONPATH": str(project)},
        )
        printed = result.stdout

    assert printed == _quoted_revisions().get(revision), (
        f"guide/schema/alembic.md quotes revision {revision!r}; autogenerate writes:\n"
        f"{printed}"
    )


def test_the_deploying_guide_names_one_migration_per_number() -> None:
    """Every block and sentence of the Deploying guide follows one project, so
    a number never names two migrations there."""
    page = (DOCS_PAGES / "guide" / "schema" / "deploying.md").read_text()
    names: dict[str, set[str]] = {}
    for name in re.findall(r"\b(\d{4})_([a-z0-9_]+)", page):
        names.setdefault(name[0], set()).add(name[1])
    assert {number: sorted(n) for number, n in names.items() if len(n) > 1} == {}
