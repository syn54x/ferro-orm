"""``ferro migrate check`` (#518): the offline CI gate.

``check`` reads the models and the migrations directory, never a database:
exit 0 when every model change has a migration and the directory is intact,
exit 3 naming each problem otherwise. The model sources and helpers are
``test_migrate_new``'s.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ferro import FerroSettings
from tests.test_migrate_new import (
    AUTHOR,
    LIBRARY,
    run,
    write_config,
    write_models,
)

pytestmark = pytest.mark.usefixtures("isolated_imports", "clean_registry")


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
    return "ferro_mcheck_" + tmp_path.name.replace("-", "_").lower()


def _generated(
    project: Path, pkg: str, body: str = AUTHOR, name: str = "create_author"
):
    write_config(project, pkg)
    write_models(project, pkg, body)
    assert run("migrate", "new", name) == 0


def test_a_clean_tree_exits_0_naming_the_head(project, pkg, capsys):
    _generated(project, pkg)
    capsys.readouterr()

    assert run("migrate", "check") == 0

    assert capsys.readouterr().out == "ok: models match 0001_create_author\n"


def test_an_ungenerated_model_exits_3_naming_it(project, pkg, capsys):
    _generated(project, pkg)
    write_models(project, pkg, LIBRARY)
    capsys.readouterr()

    assert run("migrate", "check") == 3

    err = capsys.readouterr().err
    assert err.startswith("ungenerated: "), err
    assert "since 0001_create_author" in err and "new models: Post" in err, err

    # Generating it makes the tree clean again.
    assert run("migrate", "new", "create_post") == 0
    assert run("migrate", "check") == 0


def test_a_change_new_cannot_generate_yet_is_still_ungenerated(project, pkg, capsys):
    _generated(project, pkg)
    write_models(project, pkg, AUTHOR + "    bio: str\n")
    capsys.readouterr()

    assert run("migrate", "check") == 3

    assert "not generated yet: AddColumn on author needs a backfill (ticket #534)" in (
        capsys.readouterr().err
    )


def test_no_migration_yet_is_an_ungenerated_change(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)

    assert run("migrate", "check") == 3

    err = capsys.readouterr().err
    assert "there is no migration yet" in err and "new models: Author" in err, err


def test_a_broken_chain_exits_3_naming_both_files(project, pkg, capsys):
    _generated(project, pkg)
    write_models(project, pkg, LIBRARY)
    assert run("migrate", "new", "create_post") == 0
    snapshot = project / "migrations/0002_create_post/ir.json"
    document = json.loads(snapshot.read_text())
    document["parent_checksum"] = "0" * 96  # a hand edit of 0002's ir.json
    snapshot.write_text(json.dumps(document, indent=2) + "\n")
    capsys.readouterr()

    assert run("migrate", "check") == 3

    err = capsys.readouterr().err
    assert err.startswith("broken_chain: "), err
    assert "0001_create_author/ir.json" in err and "0002_create_post/ir.json" in err


def test_an_edited_parent_snapshot_breaks_the_chain_too(project, pkg, capsys):
    _generated(project, pkg)
    write_models(project, pkg, LIBRARY)
    assert run("migrate", "new", "create_post") == 0
    first = project / "migrations/0001_create_author/ir.json"
    first.write_bytes(first.read_bytes().replace(b"\n", b"\r\n"))  # a CRLF checkout
    capsys.readouterr()

    assert run("migrate", "check") == 3

    err = capsys.readouterr().err
    assert "0001_create_author/ir.json" in err and "0002_create_post/ir.json" in err


def test_a_duplicate_number_exits_3_naming_both_directories(project, pkg, capsys):
    import shutil

    _generated(project, pkg)
    migrations = project / "migrations"
    shutil.copytree(migrations / "0001_create_author", migrations / "0001_other")
    capsys.readouterr()

    assert run("migrate", "check") == 3

    err = capsys.readouterr().err
    assert err.startswith("duplicate_number: "), err
    assert "0001_create_author" in err and "0001_other" in err, err


def test_a_missing_number_exits_3_naming_the_gap(project, pkg, capsys):
    _generated(project, pkg)
    migrations = project / "migrations"
    (migrations / "0001_create_author").rename(migrations / "0002_create_author")
    capsys.readouterr()

    assert run("migrate", "check") == 3

    err = capsys.readouterr().err
    assert err.startswith("missing_number: ") and "0001" in err, err


def test_a_rendering_the_config_requires_is_missing(project, pkg, capsys):
    write_config(project, pkg, '["postgres"]')
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "create_author") == 0
    write_config(project, pkg, '["postgres", "sqlite"]')
    capsys.readouterr()

    assert run("migrate", "check") == 3

    err = capsys.readouterr().err
    assert err.startswith("missing_rendering: "), err
    assert "0001_create_author/01_schema" in err and "sqlite" in err, err


def test_a_hand_written_step_is_not_a_problem(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "create_author", "--sql-step", "fix_rows") == 0
    capsys.readouterr()

    assert run("migrate", "check") == 0

    assert capsys.readouterr().err == ""


def test_a_config_without_dialects_is_refused_with_the_line_to_add(
    project, pkg, capsys
):
    (project / "ferro.toml").write_text(f'models = ["{pkg}.models"]\n')
    write_models(project, pkg, AUTHOR)

    assert run("migrate", "check") == 1

    assert 'dialects = ["postgres"]' in capsys.readouterr().err


def test_check_reads_no_database_and_refuses_url(project, pkg, capsys):
    _generated(project, pkg)
    capsys.readouterr()

    assert run("migrate", "check", "--url", "sqlite::memory:") == 1

    assert "--url" in capsys.readouterr().err


def test_the_python_api_returns_the_report_and_raises_on_request(project, pkg):
    from ferro.migrations.generate import MigrationsCheckError, check

    _generated(project, pkg)
    settings = FerroSettings()
    clean = check(settings, settings.database())
    assert clean.ok and clean.head == "0001_create_author" and clean.problems == []
    clean.raise_for_problems()

    write_models(project, pkg, LIBRARY)
    report = check(settings, settings.database())
    assert not report.ok
    assert [p.kind for p in report.problems] == ["ungenerated"]
    with pytest.raises(MigrationsCheckError) as raised:
        report.raise_for_problems()
    assert raised.value.report is report
