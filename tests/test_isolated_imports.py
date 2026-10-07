"""The ``isolated_imports`` fixture drops a test's project modules and
nothing else.

A test that writes a models package under ``tmp_path`` and imports it must
leave no trace of it, so the next test's package of the same shape loads
fresh. Everything else the test happened to import first stays loaded: the
first test of a run to autogenerate imports ``alembic.autogenerate`` and
``ferro.migrations.alembic`` (which registers ferro's comparator on
Alembic's ``comparators`` dispatcher), and dropping those split the process
into two copies of Alembic. Every later autogenerate then ran on the fresh
copy's dispatcher, where ferro's comparator was never registered, and wrote
stock Alembic ops for ferro's tables (and no refusal for a context missing
``ferro_options()``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.conftest import ROOT_DIR, imports_isolated_under

# Run in a fresh interpreter, so the first import of Alembic happens inside
# the isolation, the way it does for the first test of a run that
# autogenerates.
FIRST_AUTOGENERATE_THEN_ANOTHER = """
import sys

sys.path.insert(0, {root!r})
from tests.conftest import imports_isolated_under

import sqlalchemy as sa

import ferro.migrations
from ferro import Field, Model

assert "alembic" not in sys.modules


class IsoCard(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str


def autogenerate_without_ferro_options():
    from alembic.autogenerate import produce_migrations
    from alembic.migration import MigrationContext

    from ferro.migrations import get_metadata

    engine = sa.create_engine("sqlite:///" + {db!r})
    try:
        with engine.connect() as connection:
            produce_migrations(MigrationContext.configure(connection), get_metadata())
    except RuntimeError as err:
        return str(err)
    finally:
        engine.dispose()
    return "not refused"


with imports_isolated_under({tmp!r}):
    autogenerate_without_ferro_options()

print(autogenerate_without_ferro_options())
"""


def test_a_first_autogenerate_inside_the_isolation_leaves_ferros_comparator_armed(
    tmp_path: Path,
):
    code = FIRST_AUTOGENERATE_THEN_ANOTHER.format(
        root=str(ROOT_DIR), db=str(tmp_path / "iso.db"), tmp=str(tmp_path)
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    )
    assert "ferro: autogenerate refused" in out.stdout
    assert "ferro_options" in out.stdout


def test_a_project_module_is_dropped_and_sys_path_restored(tmp_path: Path):
    package = "iso_project_" + tmp_path.name.replace("-", "_").lower()
    (tmp_path / package).mkdir()
    (tmp_path / package / "__init__.py").write_text("")
    (tmp_path / package / "models.py").write_text("VALUE = 1\n")
    path = list(sys.path)

    with imports_isolated_under(tmp_path):
        sys.path.insert(0, str(tmp_path))
        __import__(f"{package}.models")
        assert f"{package}.models" in sys.modules

    assert package not in sys.modules
    assert f"{package}.models" not in sys.modules
    assert sys.path == path
