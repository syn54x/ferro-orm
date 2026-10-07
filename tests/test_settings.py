"""Project configuration: ``FerroSettings`` and model discovery (#513).

Every test writes a real ``ferro.toml`` / ``pyproject.toml`` under
``tmp_path`` and, where models are involved, real importable ``.py`` modules,
so the lookup, the refusals and ``import_models()`` / ``database_for()`` run
exactly as a developer's project would drive them. ADR-0036 and ADR-0039 are
the authority for every rule pinned here.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from ferro import DatabaseSettings, FerroSettings
from ferro.exceptions import FerroError
from ferro.settings import SettingsError


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with nothing above it that ferro could pick up.

    ``FERRO_CONFIG`` is cleared so a developer's shell cannot leak into the
    lookup; the working directory is the project root.
    """
    monkeypatch.delenv("FERRO_CONFIG", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


# -- lookup --------------------------------------------------------------------


def test_ferro_toml_single_database_gets_its_defaults(project: Path):
    _write(
        project / "ferro.toml",
        """
        models   = ["myapp.models"]
        dialects = ["postgres", "sqlite"]
        """,
    )

    settings = FerroSettings()
    db = settings.database()

    assert settings.config_path == project / "ferro.toml"
    assert isinstance(db, DatabaseSettings)
    assert db.models == ["myapp.models"]
    assert db.dialects == ["postgres", "sqlite"]
    assert db.url_env == "DATABASE_URL"
    assert db.directory == project / "migrations"
    assert db.tracking_schema is None
    assert db.ddl_lock_timeout == "5s"
    assert db.ddl_lock_timeout_seconds == 5.0


def test_pyproject_tool_ferro_is_read(project: Path):
    _write(
        project / "pyproject.toml",
        """
        [project]
        name = "myapp"

        [tool.ferro]
        models   = ["myapp.models"]
        dialects = ["sqlite"]
        url_env  = "MYAPP_DB_DSN"
        directory = "db/migrations"
        """,
    )

    db = FerroSettings().database()

    assert db.url_env == "MYAPP_DB_DSN"
    assert db.directory == project / "db" / "migrations"


def test_lookup_walks_up_from_the_working_directory(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    _write(
        project / "ferro.toml",
        """
        models   = ["myapp.models"]
        dialects = ["sqlite"]
        """,
    )
    deep = project / "src" / "myapp" / "sub"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    settings = FerroSettings()

    assert settings.config_path == project / "ferro.toml"
    assert settings.database().directory == project / "migrations"


def test_a_pyproject_without_tool_ferro_is_walked_past(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    _write(
        project / "pyproject.toml",
        """
        [tool.ferro]
        models   = ["myapp.models"]
        dialects = ["sqlite"]
        """,
    )
    sub = project / "packages" / "lib"
    _write(sub / "pyproject.toml", '[project]\nname = "lib"\n')
    monkeypatch.chdir(sub)

    assert FerroSettings().config_path == project / "pyproject.toml"


def test_no_file_anywhere_is_an_empty_settings_object_that_says_where_it_looked(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    deep = project / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    settings = FerroSettings()

    assert settings.databases == {}
    assert settings.config_path is None
    assert settings.searched[:3] == [deep, project / "a", project]
    assert settings.searched[-1] == Path(deep.anchor)
    assert len(settings.searched) == len(deep.parents) + 1


def test_no_file_refuses_when_a_database_is_asked_for(project: Path):
    settings = FerroSettings()

    with pytest.raises(SettingsError) as exc:
        settings.database()

    message = str(exc.value)
    assert "no ferro config found" in message
    assert str(project) in message
    assert "ferro.toml" in message and "[tool.ferro]" in message


def test_config_argument_selects_the_file(project: Path, tmp_path: Path):
    _write(
        project / "ferro.toml",
        """
        models   = ["nearer.models"]
        dialects = ["sqlite"]
        """,
    )
    chosen = _write(
        tmp_path / "elsewhere" / "ferro.toml",
        """
        models   = ["chosen.models"]
        dialects = ["postgres"]
        """,
    )

    settings = FerroSettings(config=chosen)

    assert settings.config_path == chosen
    assert settings.database().models == ["chosen.models"]
    assert settings.database().directory == chosen.parent / "migrations"
    assert settings.searched == []


def test_ferro_config_env_selects_and_never_layers(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write(
        project / "pyproject.toml",
        """
        [tool.ferro]
        models   = ["nearer.models"]
        dialects = ["sqlite"]
        url_env  = "NEARER_URL"
        tracking_schema = "ferro"
        """,
    )
    named = _write(
        tmp_path / "deploy" / "ferro.toml",
        """
        models   = ["named.models"]
        dialects = ["postgres"]
        """,
    )
    monkeypatch.setenv("FERRO_CONFIG", str(named))

    db = FerroSettings().database()

    assert db.models == ["named.models"]
    assert db.url_env == "DATABASE_URL"
    assert db.tracking_schema is None


def test_config_argument_wins_over_ferro_config(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env_file = _write(
        tmp_path / "env" / "ferro.toml",
        'models = ["env.models"]\ndialects = ["sqlite"]\n',
    )
    arg_file = _write(
        tmp_path / "arg" / "ferro.toml",
        'models = ["arg.models"]\ndialects = ["sqlite"]\n',
    )
    monkeypatch.setenv("FERRO_CONFIG", str(env_file))

    assert FerroSettings(config=arg_file).database().models == ["arg.models"]


def test_a_selected_pyproject_is_read_at_tool_ferro(project: Path, tmp_path: Path):
    chosen = _write(
        tmp_path / "other" / "pyproject.toml",
        """
        [tool.ferro]
        models   = ["other.models"]
        dialects = ["sqlite"]
        """,
    )

    assert FerroSettings(config=chosen).database().models == ["other.models"]


def test_a_selected_file_that_does_not_exist_is_refused(project: Path):
    with pytest.raises(SettingsError) as exc:
        FerroSettings(config=project / "missing.toml")

    assert str(project / "missing.toml") in str(exc.value)


def test_ferro_config_naming_a_missing_file_is_refused_naming_the_variable(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FERRO_CONFIG", str(project / "nope.toml"))

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert "FERRO_CONFIG" in str(exc.value)
    assert str(project / "nope.toml") in str(exc.value)


def test_a_selected_pyproject_without_tool_ferro_is_refused(
    project: Path, tmp_path: Path
):
    chosen = _write(tmp_path / "x" / "pyproject.toml", '[project]\nname = "x"\n')

    with pytest.raises(SettingsError) as exc:
        FerroSettings(config=chosen)

    assert "[tool.ferro]" in str(exc.value)
    assert str(chosen) in str(exc.value)


def test_a_selected_file_of_any_name_carrying_tool_ferro_reads_that_table(
    project: Path, tmp_path: Path
):
    chosen = _write(
        tmp_path / "ci" / "ci.toml",
        """
        [tool.ferro]
        models   = ["ci.models"]
        dialects = ["postgres"]
        """,
    )

    assert FerroSettings(config=chosen).database().models == ["ci.models"]


def test_ferro_config_naming_a_pyproject_in_the_top_level_shape_reads_it(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    chosen = _write(
        tmp_path / "deploy" / "pyproject.toml",
        'models = ["deploy.models"]\ndialects = ["sqlite"]\n',
    )
    monkeypatch.setenv("FERRO_CONFIG", str(chosen))

    db = FerroSettings().database()

    assert db.models == ["deploy.models"]
    assert db.directory == chosen.parent / "migrations"


def test_a_file_carrying_both_shapes_is_refused_naming_both(
    project: Path, tmp_path: Path
):
    chosen = _write(
        tmp_path / "both" / "ci.toml",
        """
        models   = ["top.models"]
        dialects = ["sqlite"]

        [tool.ferro]
        models   = ["tool.models"]
        dialects = ["sqlite"]
        """,
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings(config=chosen)

    message = str(exc.value)
    assert "[tool.ferro]" in message
    assert "`models`" in message and "`dialects`" in message
    assert str(chosen) in message


def test_environment_never_overrides_a_field(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    _write(project / "ferro.toml", 'models = ["m"]\ndialects = ["sqlite"]\n')
    monkeypatch.setenv("DATABASES", '{"x": {}}')
    monkeypatch.setenv("PYTHON_PATH", '["elsewhere"]')
    monkeypatch.setenv("CONFIG_PATH", "/tmp/other.toml")

    settings = FerroSettings()

    assert list(settings.databases) == ["default"]
    assert settings.python_path == []
    assert settings.config_path == project / "ferro.toml"


def test_dotenv_is_never_loaded(project: Path):
    _write(project / "ferro.toml", 'models = ["m"]\ndialects = ["sqlite"]\n')
    _write(project / ".env", 'PYTHON_PATH=["elsewhere"]\nDATABASES={}\n')

    settings = FerroSettings()

    assert settings.python_path == []
    assert list(settings.databases) == ["default"]


def test_nothing_is_cached(project: Path):
    path = _write(project / "ferro.toml", 'models = ["first"]\ndialects = ["sqlite"]\n')
    assert FerroSettings().database().models == ["first"]

    path.write_text('models = ["second"]\ndialects = ["sqlite"]\n')

    assert FerroSettings().database().models == ["second"]


# -- the two shapes ----------------------------------------------------------


def test_several_databases_parse_to_the_same_shape(project: Path):
    _write(
        project / "pyproject.toml",
        """
        [tool.ferro]
        python_path = ["src"]

        [tool.ferro.databases.a]
        models   = ["myapp.models"]
        dialects = ["postgres"]
        url_env  = "APP_DATABASE_URL"

        [tool.ferro.databases.b]
        models   = ["myapp.analytics.models"]
        dialects = ["postgres"]
        url_env  = "ANALYTICS_DATABASE_URL"
        tracking_schema = "ferro"
        ddl_lock_timeout = "500ms"
        """,
    )

    settings = FerroSettings()

    assert sorted(settings.databases) == ["a", "b"]
    a, b = settings.database("a"), settings.database("b")
    assert isinstance(a, DatabaseSettings) and isinstance(b, DatabaseSettings)
    assert a.name == "a" and b.name == "b"
    assert a.directory == project / "migrations" / "a"
    assert b.directory == project / "migrations" / "b"
    assert b.tracking_schema == "ferro"
    assert b.ddl_lock_timeout_seconds == 0.5
    assert settings.python_path == [project / "src"]


def test_ferro_toml_several_databases_use_a_databases_table(project: Path):
    _write(
        project / "ferro.toml",
        """
        [databases.app]
        models   = ["myapp.models"]
        dialects = ["sqlite"]
        directory = "db/app"
        """,
    )

    db = FerroSettings().database("app")

    assert db.directory == project / "db" / "app"
    assert FerroSettings().database() is not None  # one database: name implied


def test_database_without_a_name_refuses_when_several_naming_both(project: Path):
    _write(
        project / "ferro.toml",
        """
        [databases.app]
        models   = ["a"]
        dialects = ["sqlite"]

        [databases.analytics]
        models   = ["b"]
        dialects = ["sqlite"]
        """,
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings().database()

    message = str(exc.value)
    assert "app" in message and "analytics" in message
    assert "--database" in message


def test_an_unknown_database_name_is_refused_listing_the_configured_ones(
    project: Path,
):
    _write(project / "ferro.toml", 'models = ["m"]\ndialects = ["sqlite"]\n')

    with pytest.raises(SettingsError) as exc:
        FerroSettings().database("analytics")

    assert "analytics" in str(exc.value)
    assert "default" in str(exc.value)


@pytest.mark.parametrize(
    "key, value",
    [
        ("models", '["x"]'),
        ("dialects", '["sqlite"]'),
        ("url_env", '"X"'),
        ("directory", '"m"'),
        ("tracking_schema", '"ferro"'),
        ("ddl_lock_timeout", '"1s"'),
    ],
)
def test_a_database_key_beside_a_databases_table_is_refused_naming_the_key(
    project: Path, key: str, value: str
):
    _write(
        project / "pyproject.toml",
        f"""
        [tool.ferro]
        {key} = {value}

        [tool.ferro.databases.analytics]
        models   = ["myapp.analytics.models"]
        dialects = ["postgres"]
        """,
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    message = str(exc.value)
    assert f"`{key}`" in message
    assert "[tool.ferro.databases.<name>]" in message


# -- refusals ------------------------------------------------------------------


def test_settings_error_is_a_ferro_error():
    assert issubclass(SettingsError, FerroError)


def test_both_files_in_one_directory_are_refused_naming_both(project: Path):
    _write(project / "ferro.toml", 'models = ["m"]\ndialects = ["sqlite"]\n')
    _write(
        project / "pyproject.toml",
        '[tool.ferro]\nmodels = ["m"]\ndialects = ["sqlite"]\n',
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    message = str(exc.value)
    assert str(project / "ferro.toml") in message
    assert str(project / "pyproject.toml") in message
    assert "remove" in message.lower()


def test_a_pyproject_without_tool_ferro_beside_a_ferro_toml_is_fine(project: Path):
    _write(project / "ferro.toml", 'models = ["m"]\ndialects = ["sqlite"]\n')
    _write(project / "pyproject.toml", '[project]\nname = "x"\n')

    assert FerroSettings().config_path == project / "ferro.toml"


def test_an_unknown_key_is_refused_by_name(project: Path):
    _write(
        project / "ferro.toml",
        'models = ["m"]\ndialects = ["sqlite"]\nmodel_paths = ["x"]\n',
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert "`model_paths`" in str(exc.value)
    assert str(project / "ferro.toml") in str(exc.value)


@pytest.mark.parametrize("key", ["config_path", "searched", "config_table"])
def test_a_file_key_naming_a_lookup_attribute_is_refused_by_name(
    project: Path, key: str
):
    # These attributes come from the lookup, never from the file; a file key
    # spelled like one must not be silently shadowed.
    _write(
        project / "ferro.toml",
        f'models = ["m"]\ndialects = ["sqlite"]\n{key} = ["elsewhere"]\n',
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert f"unknown key `{key}`" in str(exc.value)


def test_an_unknown_key_inside_a_database_is_refused_by_name(project: Path):
    _write(
        project / "pyproject.toml",
        """
        [tool.ferro.databases.app]
        models   = ["m"]
        dialects = ["sqlite"]
        timeout  = "1s"
        """,
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert "`timeout`" in str(exc.value)
    assert "[tool.ferro.databases.app]" in str(exc.value)


def test_a_url_in_the_file_is_refused_as_an_unknown_key_naming_url_env(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DATABASE_URL", "sqlite::memory:")
    _write(
        project / "ferro.toml",
        'models = ["m"]\ndialects = ["sqlite"]\nurl = "sqlite:dev.db"\n',
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    message = str(exc.value)
    assert "`url`" in message
    assert "url_env" in message


def test_python_path_inside_a_database_is_refused_pointing_to_the_top(
    project: Path,
):
    _write(
        project / "ferro.toml",
        """
        [databases.app]
        models   = ["m"]
        dialects = ["sqlite"]
        python_path = ["src"]
        """,
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert "`python_path`" in str(exc.value)
    assert "top level" in str(exc.value)


@pytest.mark.parametrize(
    "toml",
    [
        'models = ["m"]\ndialects = ["sqlite"]\nunmanaged_tables = ["audit_*"]\n',
        '[databases.app]\nmodels = ["m"]\ndialects = ["sqlite"]\n'
        'unmanaged_tables = ["audit_*"]\n',
    ],
)
def test_unmanaged_tables_is_reserved_and_refused_naming_the_issue(
    project: Path, toml: str
):
    _write(project / "ferro.toml", toml)

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert "`unmanaged_tables`" in str(exc.value)
    assert "#486" in str(exc.value)


def test_malformed_toml_is_refused_naming_the_file(project: Path):
    _write(project / "ferro.toml", "models = [\n")

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert str(project / "ferro.toml") in str(exc.value)


def test_a_malformed_pyproject_on_the_walk_is_refused(project: Path):
    _write(project / "pyproject.toml", "[tool.ferro\n")

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert str(project / "pyproject.toml") in str(exc.value)


def test_missing_dialects_prints_the_line_to_add(project: Path):
    _write(project / "ferro.toml", 'models = ["myapp.models"]\n')

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert 'dialects = ["postgres"]' in str(exc.value)


def test_missing_models_prints_the_line_to_add(project: Path):
    _write(
        project / "pyproject.toml",
        '[tool.ferro.databases.app]\ndialects = ["sqlite"]\n',
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert 'models = ["' in str(exc.value)
    assert "[tool.ferro.databases.app]" in str(exc.value)


def test_an_empty_ferro_table_is_incomplete(project: Path):
    _write(project / "pyproject.toml", "[tool.ferro]\n")

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert "models" in str(exc.value) and "dialects" in str(exc.value)


def test_an_unknown_dialect_is_refused_naming_the_accepted_ones(project: Path):
    _write(project / "ferro.toml", 'models = ["m"]\ndialects = ["mysql"]\n')

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    message = str(exc.value)
    assert "dialects" in message
    assert "postgres" in message and "sqlite" in message


def test_tracking_schema_without_postgres_is_refused_naming_key_and_dialect(
    project: Path,
):
    _write(
        project / "ferro.toml",
        'models = ["m"]\ndialects = ["sqlite"]\ntracking_schema = "ferro"\n',
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    message = str(exc.value)
    assert "tracking_schema" in message
    assert "postgres" in message
    assert "sqlite" in message


@pytest.mark.parametrize(
    "dirs",
    [
        ('"shared"', '"shared"'),
        ('"migrations"', '"migrations/b"'),
        ('"x/../shared"', '"shared"'),
    ],
)
def test_overlapping_directories_are_refused(project: Path, dirs: tuple[str, str]):
    _write(
        project / "ferro.toml",
        f"""
        [databases.a]
        models   = ["a"]
        dialects = ["sqlite"]
        directory = {dirs[0]}

        [databases.b]
        models   = ["b"]
        dialects = ["sqlite"]
        directory = {dirs[1]}
        """,
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    message = str(exc.value)
    assert "`a`" in message and "`b`" in message
    assert "directory" in message


@pytest.mark.parametrize("value", ["5", "five seconds", "-1s", "0s", "1h"])
def test_an_unparseable_ddl_lock_timeout_is_refused_naming_the_forms(
    project: Path, value: str
):
    _write(
        project / "ferro.toml",
        f'models = ["m"]\ndialects = ["postgres"]\nddl_lock_timeout = "{value}"\n',
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    message = str(exc.value)
    assert "ddl_lock_timeout" in message
    assert '"500ms"' in message and '"5s"' in message and '"1m"' in message
    assert '"0"' in message


@pytest.mark.parametrize(
    "value, seconds",
    [("250ms", 0.25), ("2s", 2.0), ("1.5s", 1.5), ("1m", 60.0), ("0", 0.0)],
)
def test_ddl_lock_timeout_forms(project: Path, value: str, seconds: float):
    _write(
        project / "ferro.toml",
        f'models = ["m"]\ndialects = ["postgres"]\nddl_lock_timeout = "{value}"\n',
    )

    assert FerroSettings().database().ddl_lock_timeout_seconds == seconds


def test_a_wrongly_typed_value_is_refused_naming_the_key(project: Path):
    _write(project / "ferro.toml", 'models = "myapp.models"\ndialects = ["sqlite"]\n')

    with pytest.raises(SettingsError) as exc:
        FerroSettings()

    assert "models" in str(exc.value)
    assert str(project / "ferro.toml") in str(exc.value)


# -- url_for -----------------------------------------------------------------


def test_url_for_reads_the_named_variable(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    _write(
        project / "ferro.toml",
        'models = ["m"]\ndialects = ["sqlite"]\nurl_env = "MYAPP_DB_DSN"\n',
    )
    monkeypatch.setenv("MYAPP_DB_DSN", "sqlite:app.db")

    assert FerroSettings().database().url_for() == "sqlite:app.db"


def test_url_for_prefers_the_override(project: Path, monkeypatch: pytest.MonkeyPatch):
    _write(project / "ferro.toml", 'models = ["m"]\ndialects = ["sqlite"]\n')
    monkeypatch.setenv("DATABASE_URL", "sqlite:env.db")

    assert FerroSettings().database().url_for("sqlite:flag.db") == "sqlite:flag.db"


def test_url_for_refuses_naming_the_unset_variable(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    _write(
        project / "ferro.toml",
        'models = ["m"]\ndialects = ["sqlite"]\nurl_env = "MYAPP_DB_DSN"\n',
    )
    monkeypatch.delenv("MYAPP_DB_DSN", raising=False)

    with pytest.raises(SettingsError) as exc:
        FerroSettings().database().url_for()

    assert "MYAPP_DB_DSN" in str(exc.value)
    assert "--url" in str(exc.value)


# -- model discovery -----------------------------------------------------------


def _package(root: Path, dotted: str, source: str) -> None:
    """Write ``dotted`` as an importable module under ``root``."""
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        init = root.joinpath(*parts[:i], "__init__.py")
        if not init.exists():
            _write(init, "")
    _write(root.joinpath(*parts[:-1], parts[-1] + ".py"), source)


@pytest.fixture
def pkg(tmp_path: Path) -> str:
    """A package name unique to this test, so module caches never collide."""
    return "ferro_settings_" + tmp_path.name.replace("-", "_").lower()


def test_import_models_puts_the_config_directory_first_on_sys_path(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    _package(
        project,
        f"{pkg}.models",
        """
        from ferro import Model

        class SettingsGadget(Model):
            id: int | None = None
            name: str
        """,
    )
    _write(
        project / "ferro.toml", f'models = ["{pkg}.models"]\ndialects = ["sqlite"]\n'
    )
    sub = project / "deep"
    sub.mkdir()
    sys.path.insert(0, "/nonexistent-first")

    db = FerroSettings(config=project / "ferro.toml").database()
    models = db.import_models()

    assert [m.__name__ for m in models] == ["SettingsGadget"]
    assert sys.path[0] == str(project)
    assert FerroSettings(config=project / "ferro.toml").database_for(models[0]) == db


def test_import_models_adds_python_path_after_the_config_directory(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    _package(
        project / "src",
        f"{pkg}.models",
        """
        from ferro import Model

        class SettingsWidget(Model):
            id: int | None = None
        """,
    )
    _write(
        project / "ferro.toml",
        f'python_path = ["src"]\nmodels = ["{pkg}.models"]\ndialects = ["sqlite"]\n',
    )

    models = FerroSettings().database().import_models()

    assert [m.__name__ for m in models] == ["SettingsWidget"]
    assert sys.path[:2] == [str(project), str(project / "src")]


def test_import_models_refuses_a_module_that_does_not_import(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    _write(
        project / "ferro.toml",
        f'python_path = ["src"]\nmodels = ["{pkg}.missing"]\ndialects = ["sqlite"]\n',
    )

    with pytest.raises(SettingsError) as exc:
        FerroSettings().database().import_models()

    message = str(exc.value)
    assert f"{pkg}.missing" in message
    assert str(project) in message and str(project / "src") in message
    assert "python_path" in message


def _two_databases(project: Path, pkg: str) -> None:
    _write(
        project / "ferro.toml",
        f"""
        [databases.a]
        models   = ["{pkg}.a"]
        dialects = ["sqlite"]

        [databases.b]
        models   = ["{pkg}.b"]
        dialects = ["sqlite"]
        """,
    )


def test_import_models_returns_only_the_models_its_database_claims(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    _package(
        project,
        f"{pkg}.a",
        "from ferro import Model\n\n"
        "class SettingsAlpha(Model):\n    id: int | None = None\n",
    )
    _package(
        project,
        f"{pkg}.b",
        "from ferro import Model\n\n"
        "class SettingsBeta(Model):\n    id: int | None = None\n",
    )
    _two_databases(project, pkg)
    settings = FerroSettings()

    a_models = settings.database("a").import_models()
    b_models = settings.database("b").import_models()

    assert [m.__name__ for m in a_models] == ["SettingsAlpha"]
    assert [m.__name__ for m in b_models] == ["SettingsBeta"]
    assert settings.database_for(a_models[0]).name == "a"
    assert settings.database_for(b_models[0]).name == "b"


def test_database_for_refuses_an_unclaimed_model_naming_module_and_key(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    _package(
        project,
        f"{pkg}.stray",
        "from ferro import Model\n\n"
        "class SettingsStray(Model):\n    id: int | None = None\n",
    )
    _two_databases(project, pkg)
    sys.path.insert(0, str(project))
    stray = __import__(f"{pkg}.stray", fromlist=["SettingsStray"]).SettingsStray

    with pytest.raises(SettingsError) as exc:
        FerroSettings().database_for(stray)

    message = str(exc.value)
    assert f"{pkg}.stray" in message
    assert "models" in message
    assert "SettingsStray" in message


def test_import_models_refuses_when_a_registered_model_is_unclaimed(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    _package(
        project,
        f"{pkg}.a",
        "from ferro import Model\n"
        f"import {pkg}.stray  # noqa: F401\n\n"
        "class SettingsAlpha2(Model):\n    id: int | None = None\n",
    )
    _package(
        project,
        f"{pkg}.stray",
        "from ferro import Model\n\n"
        "class SettingsStray2(Model):\n    id: int | None = None\n",
    )
    _two_databases(project, pkg)

    with pytest.raises(SettingsError) as exc:
        FerroSettings().database("a").import_models()

    assert f"{pkg}.stray" in str(exc.value)
    assert "SettingsStray2" in str(exc.value)


def test_one_database_claims_every_registered_model(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    _package(
        project,
        f"{pkg}.elsewhere",
        "from ferro import Model\n\n"
        "class SettingsLoner(Model):\n    id: int | None = None\n",
    )
    _write(
        project / "ferro.toml", f'models = ["{pkg}.models"]\ndialects = ["sqlite"]\n'
    )
    sys.path.insert(0, str(project))
    loner = __import__(f"{pkg}.elsewhere", fromlist=["x"]).SettingsLoner

    assert FerroSettings().database_for(loner).name == "default"


def test_a_foreign_key_across_two_databases_is_refused_naming_both(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    _package(
        project,
        f"{pkg}.b",
        """
        from ferro import BackRef, Model
        from ferro.query import Relation

        class SettingsOwner(Model):
            id: int | None = None
            gizmos: Relation[list["SettingsGizmo"]] = BackRef()
        """,
    )
    _package(
        project,
        f"{pkg}.a",
        f"""
        from typing import Annotated

        from ferro import ForeignKey, Model
        from {pkg}.b import SettingsOwner

        class SettingsGizmo(Model):
            id: int | None = None
            owner: Annotated[SettingsOwner, ForeignKey(related_name="gizmos")]
        """,
    )
    _two_databases(project, pkg)
    settings = FerroSettings()

    with pytest.raises(SettingsError) as exc:
        settings.database("a").import_models()

    message = str(exc.value)
    assert "`a`" in message and "`b`" in message
    assert "SettingsGizmo" in message and "SettingsOwner" in message

    gizmo = sys.modules[f"{pkg}.a"].SettingsGizmo
    with pytest.raises(SettingsError):
        settings.database_for(gizmo)


def test_a_forward_reference_foreign_key_across_two_databases_is_refused_naming_both(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    # Database `a` names its target by string and never imports `b`'s module,
    # so the target is only found once every configured module is imported.
    _package(
        project,
        f"{pkg}.b",
        """
        from ferro import BackRef, Model
        from ferro.query import Relation

        class SettingsCustomer(Model):
            id: int | None = None
            invoices: Relation[list["SettingsInvoice"]] = BackRef()
        """,
    )
    _package(
        project,
        f"{pkg}.a",
        """
        from typing import Annotated

        from ferro import ForeignKey, Model

        class SettingsInvoice(Model):
            id: int | None = None
            customer: Annotated["SettingsCustomer", ForeignKey(related_name="invoices")]
        """,
    )
    _two_databases(project, pkg)
    settings = FerroSettings()

    with pytest.raises(SettingsError) as exc:
        settings.database("a").import_models()

    message = str(exc.value)
    assert "`a`" in message and "`b`" in message
    assert "SettingsInvoice" in message and "SettingsCustomer" in message
    assert "no configured module" not in message

    invoice = sys.modules[f"{pkg}.a"].SettingsInvoice
    with pytest.raises(SettingsError) as exc:
        settings.database_for(invoice)
    assert "`a`" in str(exc.value) and "`b`" in str(exc.value)


def test_a_forward_reference_no_configured_module_defines_is_refused(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    _package(
        project,
        f"{pkg}.a",
        """
        from typing import Annotated

        from ferro import ForeignKey, Model

        class SettingsOrphanRef(Model):
            id: int | None = None
            ghost: Annotated["SettingsGhost", ForeignKey(related_name="refs")]
        """,
    )
    _package(project, f"{pkg}.b", "")
    _two_databases(project, pkg)

    with pytest.raises(SettingsError) as exc:
        FerroSettings().database("a").import_models()

    message = str(exc.value)
    assert "SettingsGhost" in message
    assert "no configured module defines it" in message


def test_import_models_that_register_nothing_is_refused(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    _package(project, f"{pkg}.empty", "VALUE = 1\n")
    _write(project / "ferro.toml", f'models = ["{pkg}.empty"]\ndialects = ["sqlite"]\n')

    with pytest.raises(SettingsError) as exc:
        FerroSettings().database().import_models()

    message = str(exc.value)
    assert "models" in message
    assert f"{pkg}.empty" in message
    assert "drop" in message


def test_a_model_claimed_by_two_databases_may_be_shared(
    project: Path, pkg: str, isolated_imports, clean_registry
):
    _package(
        project,
        f"{pkg}.common",
        "from ferro import Model\n\n"
        "class SettingsShared(Model):\n    id: int | None = None\n",
    )
    _write(
        project / "ferro.toml",
        f"""
        [databases.a]
        models   = ["{pkg}.common"]
        dialects = ["sqlite"]

        [databases.b]
        models   = ["{pkg}"]
        dialects = ["sqlite"]
        """,
    )
    settings = FerroSettings()

    a_models = settings.database("a").import_models()
    b_models = settings.database("b").import_models()

    assert a_models == b_models
    with pytest.raises(SettingsError) as exc:
        settings.database_for(a_models[0])
    assert "`a`" in str(exc.value) and "`b`" in str(exc.value)
    assert "database(" in str(exc.value)


def test_database_for_refuses_a_class_that_is_not_a_model(project: Path):
    _write(project / "ferro.toml", 'models = ["m"]\ndialects = ["sqlite"]\n')

    with pytest.raises(SettingsError) as exc:
        FerroSettings().database_for(int)

    assert "int" in str(exc.value)
