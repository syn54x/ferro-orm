"""The ``ferro`` command and ``ferro migrate init`` (#516).

Every test drives the CLI in-process (``ferro.cli.app([...])`` or
``ferro.cli.main([...])``), never a subprocess, inside an empty project
directory under ``tmp_path``, and then reads back what a developer would:
the file ``init`` wrote (through ``FerroSettings`` and ``tomllib``), the
directory it created, and what it printed. ADR-0036 and ADR-0039 are the
authority for the file shapes; the #473 resolution for the refusal texts.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from ferro import FerroSettings
from ferro.exceptions import FerroError
from ferro.settings import SettingsError


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty project root that is the working directory, with nothing above
    it that ferro could pick up and no ``FERRO_CONFIG`` leaking in."""
    monkeypatch.delenv("FERRO_CONFIG", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """Feed ``answers`` to the prompts in order; return the prompts asked.

    Running out of answers fails the test: every prompt must be accounted for.
    """
    queue = list(answers)
    asked: list[str] = []

    def fake_input(prompt: str = "") -> str:
        asked.append(prompt)
        if not queue:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def _no_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(prompt: str = "") -> str:
        raise AssertionError(f"init prompted although flags answered: {prompt!r}")

    monkeypatch.setattr("builtins.input", refuse)


def _app() -> Callable[[list[str]], int]:
    from ferro.cli import app

    return app


PYPROJECT = textwrap.dedent(
    """\
    [project]
    name = "myapp"
    version = "1.0.0"
    dependencies = ["ferro-orm"]

    [tool.ruff]
    line-length = 100
    """
)


# -- happy paths -----------------------------------------------------------------


def test_init_from_flags_writes_a_ferro_toml_and_creates_migrations(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "ferro.toml",
            "--models",
            "myapp.models",
            "--dialects",
            "postgres,sqlite",
        ]
    )

    assert code == 0
    settings = FerroSettings()
    assert settings.config_path == project / "ferro.toml"
    database = settings.database()
    assert database.models == ["myapp.models"]
    assert database.dialects == ["postgres", "sqlite"]
    assert (project / "migrations").is_dir()
    assert database.directory == project / "migrations"
    out = capsys.readouterr().out
    assert "Wrote ferro.toml and created migrations/" in out


def test_init_appends_tool_ferro_to_an_existing_pyproject_as_text(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    (project / "pyproject.toml").write_text(PYPROJECT)
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "pyproject.toml",
            "--models",
            "myapp.models",
            "--dialects",
            "postgres,sqlite",
        ]
    )

    assert code == 0
    text = (project / "pyproject.toml").read_text()
    assert text.startswith(PYPROJECT + "\n[tool.ferro]\n")
    document = tomllib.loads(text)
    assert document["project"] == {
        "name": "myapp",
        "version": "1.0.0",
        "dependencies": ["ferro-orm"],
    }
    assert document["tool"]["ruff"] == {"line-length": 100}
    database = FerroSettings().database()
    assert database.models == ["myapp.models"]
    assert database.dialects == ["postgres", "sqlite"]
    assert (project / "migrations").is_dir()


def test_init_appends_after_a_file_without_a_trailing_newline(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    (project / "pyproject.toml").write_text(PYPROJECT.rstrip("\n"))
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "pyproject.toml",
            "--models",
            "m",
            "--dialects",
            "sqlite",
        ]
    )

    assert code == 0
    document = tomllib.loads((project / "pyproject.toml").read_text())
    assert document["tool"]["ruff"] == {"line-length": 100}
    assert document["tool"]["ferro"] == {"models": ["m"], "dialects": ["sqlite"]}


def test_the_config_file_prompt_defaults_to_pyproject_when_the_project_has_one(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    (project / "pyproject.toml").write_text(PYPROJECT)
    asked = _answers(monkeypatch, "")

    code = _app()(
        ["migrate", "init", "--models", "myapp.models", "--dialects", "postgres"]
    )

    assert code == 0
    assert asked == ["Config file [pyproject.toml]: "]
    assert FerroSettings().config_path == project / "pyproject.toml"
    assert not (project / "ferro.toml").exists()


def test_the_config_file_prompt_defaults_to_ferro_toml_without_a_pyproject(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    asked = _answers(monkeypatch, "")

    code = _app()(["migrate", "init", "--models", "m", "--dialects", "postgres"])

    assert code == 0
    assert asked == ["Config file [ferro.toml]: "]
    assert FerroSettings().config_path == project / "ferro.toml"


def test_interactive_init_with_one_database(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    (project / "pyproject.toml").write_text(PYPROJECT)
    asked = _answers(monkeypatch, "", "", "postgres,sqlite", "")

    code = _app()(["migrate", "init"])

    assert code == 0
    assert asked == [
        "Config file [pyproject.toml]: ",
        "Models module (dotted) [myapp.models]: ",
        "Target dialects [postgres]: ",
        "Another database (separate tables, own migration history)? [y/N]: ",
    ]
    database = FerroSettings().database()
    assert database.name == "default"
    assert database.models == ["myapp.models"]
    assert database.dialects == ["postgres", "sqlite"]


def test_interactive_init_with_two_databases_writes_the_databases_form(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    (project / "pyproject.toml").write_text(PYPROJECT)
    asked = _answers(
        monkeypatch,
        "",  # config file: pyproject.toml
        "myapp.models",
        "postgres",
        "y",  # another database
        "",  # first database's name: main
        "analytics",
        "myapp.analytics",
        "",  # dialects: postgres
        "n",
    )

    code = _app()(["migrate", "init"])

    assert code == 0
    assert asked == [
        "Config file [pyproject.toml]: ",
        "Models module (dotted) [myapp.models]: ",
        "Target dialects [postgres]: ",
        "Another database (separate tables, own migration history)? [y/N]: ",
        "Name of the first database [main]: ",
        "Name of the next database: ",
        "Models module (dotted): ",
        "Target dialects [postgres]: ",
        "Another database (separate tables, own migration history)? [y/N]: ",
    ]
    text = (project / "pyproject.toml").read_text()
    assert "[tool.ferro.databases.main]" in text
    assert "[tool.ferro.databases.analytics]" in text
    assert "[tool.ferro]\n" not in text
    settings = FerroSettings()
    assert set(settings.databases) == {"main", "analytics"}
    main, analytics = settings.database("main"), settings.database("analytics")
    assert main.models == ["myapp.models"]
    assert analytics.models == ["myapp.analytics"]
    assert main.url_env == "MAIN_DATABASE_URL"
    assert analytics.url_env == "ANALYTICS_DATABASE_URL"
    assert main.directory == project / "migrations" / "main"
    assert analytics.directory == project / "migrations" / "analytics"
    assert main.directory.is_dir() and analytics.directory.is_dir()
    assert "migrations/main/" in capsys.readouterr().out


def test_two_databases_in_a_ferro_toml_use_the_top_level_databases_table(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    _answers(monkeypatch, "", "a.models", "sqlite", "y", "app", "b", "b.models", "", "")

    code = _app()(["migrate", "init"])

    assert code == 0
    document = tomllib.loads((project / "ferro.toml").read_text())
    assert set(document) == {"databases"}
    assert set(FerroSettings().databases) == {"app", "b"}


def test_interactive_prompts_ask_again_on_an_invalid_answer(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    asked = _answers(
        monkeypatch,
        "",  # ferro.toml
        "",  # no models default without a pyproject: asked again
        "m",
        "mysql",  # not a dialect: asked again
        "sqlite",
        "maybe",  # not y/n: asked again
        "",
    )

    code = _app()(["migrate", "init"])

    assert code == 0
    assert asked.count("Models module (dotted): ") == 2
    assert asked.count("Target dialects [postgres]: ") == 2
    err = capsys.readouterr().err
    assert "mysql" in err and "postgres, sqlite" in err
    assert FerroSettings().database().dialects == ["sqlite"]


def test_the_database_option_names_the_one_database(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "ferro.toml",
            "--models",
            "m",
            "--dialects",
            "postgres",
            "--database",
            "app",
        ]
    )

    assert code == 0
    settings = FerroSettings()
    assert set(settings.databases) == {"app"}
    assert settings.database().directory == project / "migrations" / "app"


def test_the_global_options_parse_before_the_verb_too(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "--database",
            "app",
            "migrate",
            "init",
            "--config-file",
            "ferro.toml",
            "--models",
            "m",
            "--dialects",
            "postgres",
        ]
    )

    assert code == 0
    assert set(FerroSettings().databases) == {"app"}


def test_directory_option_writes_the_directory_key(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "ferro.toml",
            "--models",
            "m",
            "--dialects",
            "postgres",
            "--directory",
            "db/migrations",
        ]
    )

    assert code == 0
    document = tomllib.loads((project / "ferro.toml").read_text())
    assert document["directory"] == "db/migrations"
    assert FerroSettings().database().directory == project / "db" / "migrations"
    assert (project / "db" / "migrations").is_dir()
    assert not (project / "migrations").exists()


# -- refusals --------------------------------------------------------------------


@pytest.mark.parametrize("alembic_marker", ["env.py", "versions"])
def test_init_refuses_a_default_directory_holding_an_alembic_environment(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    alembic_marker: str,
):
    (project / "migrations").mkdir()
    marker = project / "migrations" / alembic_marker
    if alembic_marker == "versions":
        marker.mkdir()
    else:
        marker.write_text("# alembic\n")
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "ferro.toml",
            "--models",
            "m",
            "--dialects",
            "postgres",
        ]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert f"migrations/ holds an Alembic environment ({alembic_marker})" in err
    assert "--directory" in err
    assert not (project / "ferro.toml").exists()


def test_init_refuses_migrations_beside_an_alembic_ini(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    (project / "migrations").mkdir()
    (project / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n")
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "ferro.toml",
            "--models",
            "m",
            "--dialects",
            "postgres",
        ]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "migrations/ holds an Alembic environment (alembic.ini beside it)" in err
    assert "--directory" in err


def test_directory_option_moves_ferro_away_from_an_alembic_environment(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    (project / "migrations").mkdir()
    (project / "migrations" / "env.py").write_text("# alembic\n")
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "ferro.toml",
            "--models",
            "m",
            "--dialects",
            "postgres",
            "--directory",
            "ferro_migrations",
        ]
    )

    assert code == 0
    assert FerroSettings().database().directory == project / "ferro_migrations"


def test_init_run_twice_refuses_naming_the_existing_tool_ferro(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    (project / "pyproject.toml").write_text(PYPROJECT)
    _no_prompts(monkeypatch)
    argv = [
        "migrate",
        "init",
        "--config-file",
        "pyproject.toml",
        "--models",
        "m",
        "--dialects",
        "postgres",
    ]
    assert _app()(argv) == 0
    written = (project / "pyproject.toml").read_text()
    capsys.readouterr()

    code = _app()(argv)

    assert code == 1
    err = capsys.readouterr().err
    assert str(project / "pyproject.toml") in err
    assert "[tool.ferro]" in err
    assert (project / "pyproject.toml").read_text() == written


def test_init_refuses_an_existing_ferro_toml(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    (project / "ferro.toml").write_text('models = ["m"]\ndialects = ["sqlite"]\n')
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "ferro.toml",
            "--models",
            "m",
            "--dialects",
            "postgres",
        ]
    )

    assert code == 1
    assert str(project / "ferro.toml") in capsys.readouterr().err


def test_init_refuses_writing_a_second_config_beside_the_first(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    (project / "ferro.toml").write_text('models = ["m"]\ndialects = ["sqlite"]\n')
    (project / "pyproject.toml").write_text(PYPROJECT)
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "pyproject.toml",
            "--models",
            "m",
            "--dialects",
            "postgres",
        ]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert str(project / "ferro.toml") in err
    assert (project / "pyproject.toml").read_text() == PYPROJECT


def test_init_refuses_a_config_file_ferro_cannot_find(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "settings.toml",
            "--models",
            "m",
            "--dialects",
            "postgres",
        ]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "settings.toml" in err and "ferro.toml" in err and "pyproject.toml" in err


def test_init_refuses_an_unknown_dialect_flag(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "ferro.toml",
            "--models",
            "m",
            "--dialects",
            "postgres,mysql",
        ]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "mysql" in err and "--dialects" in err
    assert not (project / "ferro.toml").exists()


def test_init_with_no_terminal_refuses_naming_the_missing_flag(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def no_terminal(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)

    code = _app()(["migrate", "init", "--config-file", "ferro.toml"])

    assert code == 1
    assert "--models" in capsys.readouterr().err
    assert not (project / "ferro.toml").exists()


@pytest.mark.parametrize(
    ("option", "value", "fix"),
    [
        ("--url", "sqlite::memory:", "drop --url"),
        ("--config", "x.toml", "--config-file"),
    ],
)
def test_init_refuses_global_options_it_has_no_use_for(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    option: str,
    value: str,
    fix: str,
):
    _no_prompts(monkeypatch)

    code = _app()(
        [
            option,
            value,
            "migrate",
            "init",
            "--config-file",
            "ferro.toml",
            "--models",
            "m",
            "--dialects",
            "postgres",
        ]
    )

    assert code == 1
    assert fix in capsys.readouterr().err


# -- the shell: exit codes, refusals, the cli extra ------------------------------


def test_exit_code_values_are_pinned():
    from ferro.cli import exit_codes

    # #473 decision 9; #519 and #523 code against these values.
    assert exit_codes.OK == 0
    assert exit_codes.REFUSED == 1
    assert exit_codes.USAGE == 2
    assert exit_codes.PENDING == 3
    assert exit_codes.NEEDS_ATTENTION == 4


def test_render_refusal_prints_the_message_and_returns_refused(
    capsys: pytest.CaptureFixture[str],
):
    from ferro.cli import exit_codes, render_refusal

    code = render_refusal(SettingsError("no ferro config found; create ferro.toml"))

    assert code == exit_codes.REFUSED
    assert capsys.readouterr().err == "no ferro config found; create ferro.toml\n"


def test_a_settings_error_from_a_verb_renders_as_exit_1(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    (project / "pyproject.toml").write_text("[project\nname = 'broken'\n")
    _no_prompts(monkeypatch)

    code = _app()(
        [
            "migrate",
            "init",
            "--config-file",
            "pyproject.toml",
            "--models",
            "m",
            "--dialects",
            "postgres",
        ]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "is not valid TOML" in err
    assert "Traceback" not in err


def test_an_unknown_option_is_a_usage_error_exit_2(capsys: pytest.CaptureFixture[str]):
    from ferro.cli import main

    assert main(["migrate", "init", "--no-such-flag"]) == 2
    assert "--no-such-flag" in capsys.readouterr().err


def test_help_lists_migrate(capsys: pytest.CaptureFixture[str]):
    from ferro.cli import main

    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "migrate" in out
    assert "--config" in out and "--database" in out and "--url" in out


def test_without_the_cli_extra_main_prints_the_install_hint_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from ferro.cli import main

    monkeypatch.setitem(sys.modules, "cyclopts", None)

    assert main(["migrate", "init"]) == 2
    assert capsys.readouterr().err == (
        'ferro\'s CLI needs the cli extra: pip install "ferro-orm[cli]"\n'
    )


def test_ferro_and_ferro_cli_import_without_cyclopts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "cyclopts", None)
    for name in [
        m for m in sys.modules if m == "ferro.cli" or m.startswith("ferro.cli.")
    ]:
        monkeypatch.delitem(sys.modules, name)

    import ferro

    cli = importlib.import_module("ferro.cli")

    assert ferro.Model is not None
    assert cli.Global().config is None
    assert issubclass(SettingsError, FerroError)
