"""The Schema Management docs say what the code does (#539).

Four promises the pages make, each checked against the implementation:

- the two declaration styles of every migrations example generate the same
  migration, byte for byte (the ``_annotated`` twin's ``migrations/`` equals
  the assignment twin's);
- every refusal the CLI reference quotes is the text ``ferro`` prints, for
  the refusals that need no database;
- the Migrations API page documents exactly ``ferro.migrations.__all__``, and
  every identifier an API page documents resolves at runtime;
- no page says "in-house" or "Alembic territory" (the doors are named
  Auto-migrate, Migrations and Alembic).
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import ferro.migrations

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
    "migrations_testing",
]


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def _run_example(name: str, project: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(DOCS_EXAMPLES / f"{name}.py"), str(project)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"{name} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


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


def _ferro(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
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
        env={"PATH": "", "PYTHONDONTWRITEBYTECODE": "1"},
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


REFUSALS = {
    "missing-dialects": (_missing_dialects, ("migrate", "check")),
    "both-config-files": (_both_config_files, ("migrate", "check")),
    "unknown-key": (_unknown_key, ("migrate", "check")),
    "primary-key-change": (_primary_key_change, ("migrate", "new", "rekey")),
}


def _quoted_refusals() -> dict[str, str]:
    """Each ```text block the CLI reference marks ``<!-- refusal: <id> -->``."""
    page = CLI_REFERENCE.read_text()
    found = re.findall(
        r"<!-- refusal: ([a-z-]+) -->\s*```text\n(.*?)```", page, flags=re.DOTALL
    )
    return {key: text for key, text in found}


def test_the_cli_reference_quotes_each_database_free_refusal() -> None:
    assert sorted(_quoted_refusals()) == sorted(REFUSALS)


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
    documented = sorted(
        target.rsplit(".", 1)[1]
        for target in targets
        if not target.startswith("ferro.migrations.testing.")
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


def test_baseline_and_drift_are_the_functions_on_the_package() -> None:
    from ferro.migrations._baseline import baseline
    from ferro.migrations._drift import drift

    assert ferro.migrations.baseline is baseline
    assert ferro.migrations.drift is drift


def test_the_api_page_documents_the_test_harness() -> None:
    page = API_PAGE.read_text()
    for name in ("harness", "Harness", "RoundTripResult"):
        assert f"::: ferro.migrations.testing.{name}\n" in page, name


# -- the doors' names ---------------------------------------------------------------


@pytest.mark.parametrize("phrase", ["in-house", "Alembic territory"])
def test_no_page_names_a_door_the_old_way(phrase: str) -> None:
    hits = [
        f"{path.relative_to(DOCS_PAGES)}:{number}"
        for path in sorted(DOCS_PAGES.rglob("*.md"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if phrase in line
    ]
    assert hits == []
