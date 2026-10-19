"""The testing how-to's fixtures, run the way the page tells readers to.

``docs/pages/howto/testing.md`` hands readers a ``tests/conftest.py``
(``docs/examples/testing_conftest.py``), a pytest configuration and a few
test modules. Each test here builds that project in a temporary directory:

- ``myapp/models.py`` from ``docs/examples/testing_models.py``,
- ``tests/conftest.py`` from ``docs/examples/testing_conftest.py``,
- ``pyproject.toml`` from the page's ``[tool.pytest.ini_options]`` block,
- every page fence marked ``{test=pytest path="tests/<file>.py"}`` at its path,

then runs pytest there in a subprocess, so the fixtures run under the
pytest-asyncio mode the page configures, not this suite's.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._docs_harness import fence_settings, fenced_examples

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE = REPO_ROOT / "docs" / "pages" / "howto" / "testing.md"
EXAMPLES = REPO_ROOT / "docs" / "examples"

_TOML_FENCE = re.compile(r"^```toml\n(.*?)^```", flags=re.M | re.S)
_PYTEST_OUTPUT = re.compile(
    r"^```text\n\$ pytest -q (?P<target>\S+)\n.*?^(?P<passed>\d+) passed",
    flags=re.M | re.S,
)


def _page_test_modules() -> dict[str, str]:
    """The page's pytest modules: path in the project -> source."""
    text = PAGE.read_text(encoding="utf-8")
    modules = {}
    for example in fenced_examples(PAGE, text):
        settings = fence_settings(example)
        if settings.get("test") == "pytest":
            modules[settings["path"]] = example.source
    return modules


def _write_project(root: Path, extra: dict[str, str] | None = None) -> None:
    text = PAGE.read_text(encoding="utf-8")
    [pyproject] = [
        block
        for block in _TOML_FENCE.findall(text)
        if "[tool.pytest.ini_options]" in block
    ]
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (root / "myapp").mkdir()
    (root / "myapp" / "__init__.py").write_text("", encoding="utf-8")
    (root / "myapp" / "models.py").write_text(
        (EXAMPLES / "testing_models.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        (EXAMPLES / "testing_conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    for path, source in {**_page_test_modules(), **(extra or {})}.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")


def _pytest(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=root,
        env={**os.environ, **(env or {})},
    )
    assert result.returncode == 0, (
        f"pytest failed in the testing how-to's project\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_page_has_its_pytest_modules() -> None:
    assert set(_page_test_modules()) == {
        "tests/test_users.py",
        "tests/test_raw_sql.py",
        "tests/test_posts.py",
    }


def test_page_tests_pass_with_the_page_fixtures(tmp_path: Path) -> None:
    _write_project(tmp_path)

    stdout = _pytest(tmp_path)

    assert "4 passed" in stdout, stdout


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
    _write_project(tmp_path, {"tests/test_rollback_probe.py": ROLLBACK_PROBE})

    stdout = _pytest(tmp_path, "tests/test_rollback_probe.py")

    assert "1 passed" in stdout, stdout


def test_db_gives_each_test_an_empty_database(tmp_path: Path) -> None:
    _write_project(tmp_path, {"tests/test_isolation_probe.py": ISOLATION_PROBE})

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
