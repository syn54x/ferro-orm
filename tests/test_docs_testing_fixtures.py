"""The ``test=pytest`` fences of the docs pages, run the way the pages tell readers to.

``docs/pages/howto/testing.md`` hands readers a ``tests/conftest.py``
(``docs/examples/testing_conftest.py``), a pytest configuration and a few
test modules; any other page may mark a fence
``{test=pytest path="tests/<file>.py"}`` too. Each test here builds one
project per page in a temporary directory:

- ``myapp/models.py`` from the page's example module by slug,
  ``docs/examples/<page-stem-with-underscores>.py`` (the how-to's is
  ``testing_models.py``),
- ``tests/conftest.py`` from ``docs/examples/testing_conftest.py``,
- ``pyproject.toml`` from the how-to's ``[tool.pytest.ini_options]`` block,
- every fence of the page marked ``test=pytest`` at its path,

then runs pytest there in a subprocess, so the fixtures run under the
pytest-asyncio mode the how-to configures, not this suite's.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest

from tests._docs_harness import (
    fence_settings,
    fenced_blocks,
    fenced_examples,
    run_python,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGES = REPO_ROOT / "docs" / "pages"
PAGE = PAGES / "howto" / "testing.md"
INSPECTING_SQL = PAGES / "guide" / "inspecting-sql.md"
EXAMPLES = REPO_ROOT / "docs" / "examples"

# The how-to's models predate the slug rule and keep their name.
_MODELS_BY_PAGE = {PAGE: EXAMPLES / "testing_models.py"}

_PYTEST_OUTPUT = re.compile(
    r"^```text\n\$ pytest -q (?P<target>\S+)\n.*?^(?P<passed>\d+) passed",
    flags=re.M | re.S,
)


def _page_test_modules(page: Path) -> dict[str, str]:
    """The page's pytest modules: path in the project -> source."""
    text = page.read_text(encoding="utf-8")
    modules = {}
    for example in fenced_examples(page, text):
        settings = fence_settings(example)
        if settings.get("test") == "pytest":
            path = settings["path"]
            parts = PurePosixPath(path).parts
            if parts[:1] != ("tests",) or ".." in parts:
                raise ValueError(
                    f"{example}: path={path} is outside tests/; "
                    'write the fence {test=pytest path="tests/<file>.py"}'
                )
            modules[path] = example.source
    return modules


def pages_with_pytest_fences() -> list[Path]:
    """Every docs page that owns at least one ``test=pytest`` fence."""
    return [
        page
        for page in sorted(PAGES.rglob("*.md"))
        if "test=pytest" in page.read_text(encoding="utf-8")
        and _page_test_modules(page)
    ]


def models_module_for(page: Path) -> Path:
    """The example module that is the page's ``myapp/models.py``, by slug."""
    if page in _MODELS_BY_PAGE:
        return _MODELS_BY_PAGE[page]
    slug = page.stem.replace("-", "_")
    module = EXAMPLES / f"{slug}.py"
    if not module.is_file():
        raise FileNotFoundError(
            f"{page}: a test=pytest fence needs docs/examples/{slug}.py as its "
            "myapp/models.py; add it or move the fence to test=compile"
        )
    return module


def _write_project(
    root: Path, page: Path = PAGE, extra: dict[str, str] | None = None
) -> None:
    text = PAGE.read_text(encoding="utf-8")
    [pyproject] = [
        fence.body
        for fence in fenced_blocks(PAGE, text, "toml")
        if "[tool.pytest.ini_options]" in fence.body
    ]
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (root / "myapp").mkdir()
    (root / "myapp" / "__init__.py").write_text("", encoding="utf-8")
    (root / "myapp" / "models.py").write_text(
        models_module_for(page).read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        (EXAMPLES / "testing_conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    for path, source in {**_page_test_modules(page), **(extra or {})}.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")


def _pytest(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = run_python(
        ["-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        timeout=120,
        cwd=root,
        env=env,
    )
    return result.stdout


EXPECTED_MODULES = {
    PAGE: {"tests/test_users.py", "tests/test_raw_sql.py", "tests/test_posts.py"},
    INSPECTING_SQL: {"tests/test_statement_log.py"},
}
EXPECTED_PASSED = {PAGE: 4, INSPECTING_SQL: 1}
EXPECTED_MODEL = {PAGE: "User", INSPECTING_SQL: "Account"}


def test_pages_with_pytest_fences_are_the_known_pages() -> None:
    assert set(pages_with_pytest_fences()) == set(EXPECTED_MODULES)


@pytest.mark.parametrize("page", EXPECTED_MODULES, ids=lambda page: page.stem)
def test_page_has_its_pytest_modules(page: Path) -> None:
    assert set(_page_test_modules(page)) == EXPECTED_MODULES[page]


@pytest.mark.parametrize("page", EXPECTED_MODULES, ids=lambda page: page.stem)
def test_page_tests_pass_with_the_page_fixtures(page: Path, tmp_path: Path) -> None:
    _write_project(tmp_path, page)

    stdout = _pytest(tmp_path)

    assert f"{EXPECTED_PASSED[page]} passed" in stdout, stdout


@pytest.mark.parametrize("page", EXPECTED_MODULES, ids=lambda page: page.stem)
def test_page_models_import_without_running_the_example(
    page: Path, tmp_path: Path
) -> None:
    _write_project(tmp_path, page)
    code = (
        "import ferro\n"
        "def refuse(*args, **kwargs):\n"
        "    raise AssertionError('importing myapp.models connected')\n"
        "ferro.connect = refuse\n"
        "import myapp.models\n"
        "from ferro.registry import REGISTRY\n"
        "names = [m.__name__ for m in REGISTRY.models().values()]\n"
        f"assert {EXPECTED_MODEL[page]!r} in names, names\n"
    )

    result = run_python(["-c", code], timeout=60, cwd=tmp_path)

    assert result.returncode == 0, result.stderr


def test_page_without_a_slug_module_names_the_module_to_add(tmp_path: Path) -> None:
    page = tmp_path / "no-such-example.md"
    page.write_text(
        '```python {test=pytest path="tests/test_x.py"}\nassert True\n```\n',
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(FileNotFoundError) as raised:
        _write_project(project, page)

    assert str(raised.value) == (
        f"{page}: a test=pytest fence needs docs/examples/no_such_example.py as its "
        "myapp/models.py; add it or move the fence to test=compile"
    )


@pytest.mark.parametrize("path", ["src/x.py", "tests/../x.py"])
def test_pytest_fence_outside_tests_is_refused(tmp_path: Path, path: str) -> None:
    page = tmp_path / "stray.md"
    page.write_text(
        f'```python {{test=pytest path="{path}"}}\nassert True\n```\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="outside tests/"):
        _page_test_modules(page)


def test_page_shows_what_pytest_prints(tmp_path: Path) -> None:
    _write_project(tmp_path)
    shown = _PYTEST_OUTPUT.search(PAGE.read_text(encoding="utf-8"))
    assert shown is not None, "the page shows no `$ pytest -q <file>` output"

    stdout = _pytest(tmp_path, shown["target"])

    assert f"{shown['passed']} passed" in stdout, stdout


ROLLBACK_PROBE = '''
import pytest

from myapp.models import User


@pytest.fixture
async def users_after_test(db):
    """Torn down after db_transaction and before db: counts what was kept."""
    yield
    remaining = await User.select().count()
    assert remaining == 0, f"db_transaction kept {remaining} row(s)"


async def test_row_created_in_the_transaction(users_after_test, db_transaction):
    await User.create(username="ada", email="ada@example.com")
    row = await db_transaction.fetch_one('SELECT count(*) AS total FROM "user"')
    assert row == {"total": 1}
'''

ISOLATION_PROBE = """
from myapp.models import User


async def test_first_ada(db):
    await User.create(username="ada", email="ada@example.com")
    assert await User.select().count() == 1


async def test_second_ada(db):
    await User.create(username="ada", email="ada@example.com")
    assert await User.select().count() == 1
"""


def test_db_transaction_rolls_the_row_back(tmp_path: Path) -> None:
    _write_project(tmp_path, extra={"tests/test_rollback_probe.py": ROLLBACK_PROBE})

    stdout = _pytest(tmp_path, "tests/test_rollback_probe.py")

    assert "1 passed" in stdout, stdout


def test_db_gives_each_test_an_empty_database(tmp_path: Path) -> None:
    _write_project(tmp_path, extra={"tests/test_isolation_probe.py": ISOLATION_PROBE})

    stdout = _pytest(tmp_path, "tests/test_isolation_probe.py")

    assert "2 passed" in stdout, stdout


POSTGRES_PROBE = """
from myapp.models import User


async def test_user_in_its_own_schema(pg_db):
    await User.create(username="ada", email="ada@example.com")
    assert await User.select().count() == 1
"""


@pytest.mark.postgres_only
def test_pg_db_isolates_each_test_in_a_schema(
    tmp_path: Path, postgres_base_url: str | None
) -> None:
    if postgres_base_url is None:
        pytest.skip("No Postgres provider is configured.")
    _write_project(tmp_path)
    pg_dir = tmp_path / "tests" / "postgres"
    pg_dir.mkdir()
    (pg_dir / "conftest.py").write_text(
        (EXAMPLES / "testing_conftest_postgres.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (pg_dir / "test_pg.py").write_text(POSTGRES_PROBE, encoding="utf-8")

    stdout = _pytest(
        tmp_path,
        "tests/postgres",
        env={"TEST_POSTGRES_URL": postgres_base_url},
    )

    assert "1 passed" in stdout, stdout
