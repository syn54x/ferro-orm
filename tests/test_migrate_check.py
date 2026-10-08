"""``ferro migrate check`` (#518): the offline CI gate.

``check`` reads the models and the migrations directory, never a database:
exit 0 when every model change has a migration and the directory is intact,
exit 3 naming each problem otherwise. The model sources and helpers are
``test_migrate_new``'s.
"""

from __future__ import annotations

import asyncio
import json
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


def test_a_label_added_on_a_sqlite_only_project_is_recorded_and_check_is_clean(
    project, pkg, capsys
):
    """SQLite keeps an enum label as text, so it has nothing to run for a
    new one, but the models changed: ``new`` writes the ``not-applicable``
    labels step and stores the target snapshot, and ``check`` is clean."""
    write_config(project, pkg, '["sqlite"]')
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "create_author") == 0
    added = AUTHOR.replace('    LIVE = "live"', '    LIVE = "live"\n    DEAD = "dead"')
    write_models(project, pkg, added)
    capsys.readouterr()
    assert run("migrate", "check") == 3
    assert "new enum labels: status.dead" in capsys.readouterr().err

    assert run("migrate", "new", "status_dead") == 0
    migration = next((project / "migrations").glob("0002_*"))
    for name in ("01_labels.up.sqlite.sql", "01_labels.down.sqlite.sql"):
        assert (migration / name).read_text() == "-- ferro: not-applicable\n"
    stored = json.loads((migration / "ir.json").read_text())
    assert "dead" in json.dumps(stored)
    capsys.readouterr()
    assert run("migrate", "check") == 0
    assert capsys.readouterr().out == "ok: models match 0002_status_dead\n"


def test_a_change_new_cannot_generate_yet_is_still_ungenerated(project, pkg, capsys):
    _generated(project, pkg)
    write_models(project, pkg, AUTHOR.replace("    status: Status", "    status: str"))
    capsys.readouterr()

    assert run("migrate", "check") == 3

    err = capsys.readouterr().err
    assert (
        'cannot generate it: "author"."status" moves to or from a native enum type, '
        "which no statement converts in place"
    ) in err, err


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


def test_an_unwritten_data_step_exits_3_naming_each_todo(project, pkg, capsys):
    _generated(project, pkg)
    assert run("migrate", "new", "backfill", "--data-step", "Author") == 0
    capsys.readouterr()

    assert run("migrate", "check") == 3

    assert capsys.readouterr().err.splitlines() == [
        "unwritten_step: 0002_backfill/01_backfill_author.py:7: not written yet: "
        "write this step",
        "unwritten_step: 0002_backfill/01_backfill_author.py:12: not written yet: "
        "write this step",
    ]

    step = project / "migrations" / "0002_backfill" / "01_backfill_author.py"
    step.write_text(
        "from ferro.migrations import atomic, nothing_to_reverse\n\n\n"
        "@atomic\nasync def up(ctx):\n    pass\n\n\n"
        '@nothing_to_reverse("nothing was changed")\ndef down(ctx): ...\n'
    )
    assert run("migrate", "check") == 0
    assert capsys.readouterr().out == "ok: models match 0002_backfill\n"


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
    clean = asyncio.run(check(settings))
    assert clean.ok and clean.head == "0001_create_author" and clean.problems == []
    clean.raise_for_problems()

    write_models(project, pkg, LIBRARY)
    report = asyncio.run(check(settings))
    assert not report.ok
    assert [p.kind for p in report.problems] == ["ungenerated"]
    with pytest.raises(MigrationsCheckError) as raised:
        report.raise_for_problems()
    assert raised.value.report is report
