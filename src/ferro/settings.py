"""Project configuration: the one way every ferro tool reads a project's databases.

A project commits one file that names its databases (ADR-0036, ADR-0039):

```toml
# ferro.toml — or the same keys under [tool.ferro] in pyproject.toml
models   = ["myapp.models"]
dialects = ["postgres", "sqlite"]
```

```python
from ferro import FerroSettings

settings = FerroSettings()
db = settings.database()           # implied: there is one
db.directory                       # <config dir>/migrations
db.url_for()                       # $DATABASE_URL, never the file
db.import_models()                 # imports myapp.models, returns its models
```

The rules, each enforced here with a refusal that names its fix:

- **Lookup.** ``FerroSettings(config=…)``, else ``FERRO_CONFIG``, else the
  nearest directory, walking up from the working directory, holding a
  ``ferro.toml`` or a ``pyproject.toml`` with a ``[tool.ferro]`` table. A
  directory holding both is refused. A selected file is used alone: nothing
  is layered on it.
- **The file is the only source.** No environment variable overrides a key
  and no ``.env`` is loaded; ``settings_customise_sources`` reads the
  located file with pydantic-settings' ``TomlConfigSettingsSource`` and
  keeps no other source. The database URL is never in the
  file: ``url_for`` takes an override or reads the variable ``url_env``
  names.
- **No file is not an error** for ``FerroSettings()``: it returns an empty
  object that says where it searched, and refuses only when a database is
  asked for. A malformed or incomplete file is an error for every consumer.
- **A model's database is its defining module.** With one database, every
  registered model is in it. With several, a model belongs to each database
  whose ``models`` list holds its ``__module__`` or a parent of it; a model
  no database claims is refused, and so is a foreign key from a model to one
  its database does not claim.
- **Nothing is cached.** Each ``FerroSettings()`` reads the file again.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from collections.abc import Sequence
from typing import Any, ForwardRef, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from .base import ForeignKey, ManyToManyRelation
from .exceptions import FerroError

__all__ = ["DatabaseSettings", "FerroSettings", "SettingsError"]

Dialect = Literal["postgres", "sqlite"]

FERRO_TOML = "ferro.toml"
PYPROJECT_TOML = "pyproject.toml"
CONFIG_ENV = "FERRO_CONFIG"
DEFAULT_DATABASE = "default"
"""The name of the one database a config declares without a databases table."""
DEFAULT_DIRECTORY = "migrations"

_DATABASE_KEYS = (
    "models",
    "dialects",
    "url_env",
    "directory",
    "tracking_schema",
    "ddl_lock_timeout",
    "lock_timeout",
)
_TOP_LEVEL_KEYS = ("python_path", "databases")
_RESERVED_KEYS = {
    "unmanaged_tables": (
        "is reserved for the list of live tables ferro leaves alone, which has "
        "not shipped yet (https://github.com/syn54x/ferro-orm/issues/486); "
        "remove it until #486 lands"
    ),
}
_FERRO_KEYS = frozenset(_DATABASE_KEYS + _TOP_LEVEL_KEYS) | frozenset(_RESERVED_KEYS)
"""Keys that mark a file's top level as ferro config."""
_LINE_TO_ADD = {
    "models": 'models = ["myapp.models"]',
    "dialects": 'dialects = ["postgres"]',
}
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m)$")
_DURATION_UNITS = {"ms": "milliseconds", "s": "seconds", "m": "minutes"}
_DURATION_FORMS = '"500ms", "5s" or "1m"'


class SettingsError(FerroError):
    """The project configuration is missing, malformed, or contradicts itself.

    Every message names the fix: the key to add or move, the line to write,
    both paths found, or the variable to set.
    """


def parse_ddl_lock_timeout(value: str) -> timedelta:
    """Parse a ``ddl_lock_timeout`` duration (``"500ms"``, ``"5s"``, ``"1m"``),
    or ``"0"``, which turns the timeout and its retries off (ADR-0044)."""
    if value == "0":
        return timedelta(0)
    match = _DURATION.match(value) if isinstance(value, str) else None
    if match is None or float(match.group(1)) <= 0:
        raise ValueError(
            f"ddl_lock_timeout must be a positive duration written as "
            f'{_DURATION_FORMS}, or "0" to wait without a limit; got {value!r}'
        )
    return timedelta(**{_DURATION_UNITS[match.group(2)]: float(match.group(1))})


MAX_LOCK_TIMEOUT_S = 60.0 * 60 * 24 * 365
"""The longest lock timeout accepted: one year, "until it is free" while
staying far inside the runtime's range. A configured ``lock_timeout = "0"``
waits this long."""


def parse_lock_timeout(value: str | float) -> float:
    """Seconds from ``"30s"``, ``"500ms"``, ``"1m"`` or a plain number of
    seconds, as ``--lock-timeout`` takes them (``0`` tries the lock once,
    :data:`MAX_LOCK_TIMEOUT_S` is the most).

    Raises:
        SettingsError: ``value`` is not a duration, is negative, or is longer
            than :data:`MAX_LOCK_TIMEOUT_S`.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str) and re.fullmatch(r"\d+(\.\d+)?", value.strip()):
        seconds = float(value)
    elif isinstance(value, str) and (match := _DURATION.match(value.strip())):
        unit = _DURATION_UNITS[match.group(2)]
        seconds = (
            float(match.group(1))
            * {
                "milliseconds": 0.001,
                "seconds": 1.0,
                "minutes": 60.0,
            }[unit]
        )
    else:
        raise SettingsError(
            f'lock timeout {value!r} is not a duration; write it as "30s", '
            f'"500ms", "1m" or a number of seconds (0 refuses at once)'
        )
    if seconds < 0:
        raise SettingsError(f"lock timeout {value!r} is negative; use 0 or more")
    if not seconds <= MAX_LOCK_TIMEOUT_S:  # also refuses nan
        raise SettingsError(
            f"lock timeout {value!r} is too long; use at most one year "
            f"({MAX_LOCK_TIMEOUT_S:.0f} seconds)"
        )
    return seconds


def lock_timeout_setting_seconds(value: str) -> float:
    """A configured ``lock_timeout`` in seconds: the forms ``--lock-timeout``
    takes, except that ``"0"`` waits without a limit
    (:data:`MAX_LOCK_TIMEOUT_S`): a setting that refused every contended
    run would be no wait at all.

    Raises:
        ValueError: ``value`` is not one of those forms.
    """
    try:
        seconds = parse_lock_timeout(value)
    except SettingsError:
        raise ValueError(
            f'lock_timeout must be a duration written as "500ms", "30s", "1m" or a '
            f'number of seconds, or "0" to wait without a limit; got {value!r}'
        ) from None
    return MAX_LOCK_TIMEOUT_S if seconds == 0 else seconds


@dataclass(frozen=True)
class _Project:
    """Where a database's config came from, shared by every database in it.

    Equality covers the file and its ``python_path`` only; ``databases`` is
    the sibling view ownership checks need and would recurse if compared.
    """

    config_path: Path
    python_path: tuple[Path, ...]
    databases: dict[str, DatabaseSettings] = field(compare=False, repr=False)


class DatabaseSettings(BaseModel):
    """One configured *database*: a named set of models with one migration lineage.

    Built by :class:`FerroSettings` from ``[tool.ferro]`` (one database, named
    ``"default"``) or ``[tool.ferro.databases.<name>]`` (several). Paths are
    already resolved against the config file's directory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    models: list[str] = Field(min_length=1)
    dialects: list[Dialect] = Field(min_length=1)
    url_env: str = Field(default="DATABASE_URL", min_length=1)
    directory: Path
    tracking_schema: str | None = Field(default=None, min_length=1)
    ddl_lock_timeout: str = "5s"
    lock_timeout: str = "30s"

    _project: _Project | None = PrivateAttr(default=None)

    @field_validator("ddl_lock_timeout")
    @classmethod
    def _parseable_duration(cls, value: str) -> str:
        parse_ddl_lock_timeout(value)
        return value

    @field_validator("lock_timeout")
    @classmethod
    def _parseable_lock_timeout(cls, value: str) -> str:
        lock_timeout_setting_seconds(value)
        return value

    @model_validator(mode="after")
    def _tracking_schema_needs_postgres(self) -> DatabaseSettings:
        if self.tracking_schema is not None and "postgres" not in self.dialects:
            raise ValueError(
                f"tracking_schema is Postgres-only, but this database's dialects "
                f'are {_toml_list(self.dialects)}; add "postgres" to dialects '
                f"or remove tracking_schema"
            )
        return self

    @property
    def ddl_lock_timeout_seconds(self) -> float:
        """``ddl_lock_timeout`` in seconds; ``0.0`` when it is off."""
        return parse_ddl_lock_timeout(self.ddl_lock_timeout).total_seconds()

    @property
    def lock_timeout_seconds(self) -> float:
        """``lock_timeout`` in seconds: how long a run (``ferro migrate up``,
        ``down``, ``baseline``, ``rerecord``) or an auto-migrate pass waits
        for the run lock another one holds; ``"0"`` is
        :data:`MAX_LOCK_TIMEOUT_S`, no limit."""
        return lock_timeout_setting_seconds(self.lock_timeout)

    def lock_wait(self, override: str | float | None = None) -> float:
        """Seconds a run on this database waits for the run lock: ``override``
        (``--lock-timeout``, or a call's ``lock_timeout=``) when given, else
        the configured ``lock_timeout``.

        Raises:
            SettingsError: ``override`` is not a lock timeout.
        """
        if override is None:
            return self.lock_timeout_seconds
        return parse_lock_timeout(override)

    def url_for(self, override: str | None = None) -> str:
        """The database URL: ``override`` (``--url``), else ``$<url_env>``.

        The file never holds a URL; an unset or empty variable is refused by
        name.
        """
        if override:
            return override
        url = os.environ.get(self.url_env)
        if not url:
            raise SettingsError(
                f"database `{self.name}` reads its URL from ${self.url_env}, which "
                f"is not set; export {self.url_env}=<url> or pass --url"
            )
        return url

    def import_models(self) -> list[type]:
        """Import this database's ``models`` modules and return its models.

        The config file's directory goes first on ``sys.path``, then each
        ``python_path`` entry, and each module is imported as written. Those
        ``sys.path`` entries stay for the rest of the process, since an
        imported module may import its siblings later. The result is every
        registered model this database claims, in registration order, after
        checking that every registered model has a database and that none of
        this database's models has a foreign key into another. A foreign key
        naming its target by string is resolved against every configured
        database's modules, importing them if needed.

        An empty result is refused: no models is never read as "drop every
        table" (ADR-0036).
        """
        project = self._require_project()
        _import_database_modules(project, self)
        models = _models_of(project, self)
        if not models:
            modules = ", ".join(f"`{module}`" for module in self.models)
            raise SettingsError(
                f"database `{self.name}` imported {modules} ({project.config_path}) "
                f"and no model was registered from them; an empty modelset is "
                f"never read as 'drop every table'. Point the models key at the "
                f"modules that define this database's Model classes"
            )
        return models

    def _require_project(self) -> _Project:
        if self._project is None:
            raise SettingsError(
                f"database `{self.name}` was built by hand and has no project "
                f"configuration to import from; take it from FerroSettings().database()"
            )
        return self._project


class FerroSettings(BaseSettings):
    """A project's ferro configuration, read from its one config file.

    ``FerroSettings()`` finds the file (see the module docstring for the
    lookup); ``FerroSettings(config=path)`` names it. With no file found the
    object is empty: ``databases == {}`` and ``searched`` lists every
    directory examined.

    The lookup is ferro's; reading the file is pydantic-settings':
    ``__init__`` hands the located path to ``settings_customise_sources``
    through the init source, which then reads it with one
    ``TomlConfigSettingsSource`` and nothing else. The validators below shape
    the file's keys (one database or a ``databases`` table) into
    ``databases`` and refuse what does not belong, naming where it goes.
    """

    model_config = SettingsConfigDict(extra="forbid", frozen=True, case_sensitive=True)

    config_path: Path | None = None
    """The config file read, or ``None`` when none was found."""
    config_table: tuple[str, ...] = ()
    """Where the keys sit in it: ``("tool", "ferro")``, or ``()`` for the top level."""
    searched: list[Path] = Field(default_factory=list)
    """Every directory the walk-up examined without finding a config."""
    python_path: list[Path] = Field(default_factory=list)
    databases: dict[str, DatabaseSettings] = Field(default_factory=dict)

    def __init__(self, config: Path | str | None = None) -> None:
        path, searched = _locate(config)
        if path is None:
            super().__init__(searched=searched)
            return
        table = _config_table(path)
        try:
            super().__init__(config_path=path, config_table=table, searched=searched)
        except ValidationError as exc:
            raise SettingsError(_describe(_Layout(path, table), exc)) from None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """The located config file, read by ``TomlConfigSettingsSource``.

        ``init_settings`` carries only what ``__init__`` located (the path,
        its table header, the directories searched). No environment
        variable, ``.env`` file or secrets directory ever sets a field
        (ADR-0036: these keys decide what a migration contains).
        """
        located = (
            init_settings.init_kwargs
            if isinstance(init_settings, InitSettingsSource)
            else {}
        )
        path = located.get("config_path")
        if path is None:
            return (init_settings,)
        header = located.get("config_table", ())
        try:
            file_settings = TomlConfigSettingsSource(
                settings_cls, toml_file=path, toml_table_header=header
            )
        except tomllib.TOMLDecodeError as exc:
            raise SettingsError(
                f"{path} is not valid TOML: {exc}; fix the file"
            ) from None
        layout = _Layout(path, header)
        for key in _LOCATED_FIELDS:
            if key in file_settings.toml_data:
                layout.refuse_unknown(key, layout.top_label, allowed=_TOP_LEVEL_KEYS)
        return (init_settings, file_settings)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        """Shape the file's keys into ``python_path`` and ``databases``.

        Every key-level refusal (unknown, reserved, misplaced, missing) is
        decided here, where the layout is known, so its message says where
        the key goes. Type errors are left to field validation.
        """
        if not isinstance(data, dict) or data.get("config_path") is None:
            return data
        layout = _Layout(Path(data["config_path"]), tuple(data.get("config_table", ())))
        located = {key: data[key] for key in _LOCATED_FIELDS if key in data}
        keys = {key: value for key, value in data.items() if key not in _LOCATED_FIELDS}
        if not layout.header and not any(key in _FERRO_KEYS for key in keys):
            raise SettingsError(
                f"{layout.path} holds no ferro config: no [tool.ferro] table and "
                f"no ferro keys at the top level. Add a [tool.ferro] table with "
                f"models = [...] and dialects = [...], or write those keys at "
                f"the top level of a {FERRO_TOML}"
            )
        if "databases" in keys:
            entries = layout.several(keys)
            defaults = {name: Path(DEFAULT_DIRECTORY, name) for name in entries}
        else:
            single = {key: value for key, value in keys.items() if key != "python_path"}
            entries = {DEFAULT_DATABASE: (layout.top_label, single)}
            defaults = {DEFAULT_DATABASE: Path(DEFAULT_DIRECTORY)}
        databases = {
            name: layout.database(
                name, label, entry, layout.path.parent / defaults[name]
            )
            for name, (label, entry) in entries.items()
        }
        shaped = {**located, "databases": databases}
        if "python_path" in keys:
            shaped["python_path"] = keys["python_path"]
        return shaped

    @field_validator("python_path", mode="after")
    @classmethod
    def _relative_to_config(cls, value: list[Path], info: ValidationInfo) -> list[Path]:
        """``python_path`` entries are relative to the config file's directory."""
        config_path = info.data.get("config_path")
        if config_path is None:
            return value
        return [(config_path.parent / entry).resolve() for entry in value]

    @model_validator(mode="after")
    def _bind_databases(self) -> FerroSettings:
        """Refuse overlapping directories; give each database its project."""
        if self.config_path is None:
            return self
        _refuse_overlapping_directories(self.config_path, self.databases)
        project = _Project(self.config_path, tuple(self.python_path), self.databases)
        for database in self.databases.values():
            database._project = project
        return self

    def database(self, name: str | None = None) -> DatabaseSettings:
        """The database called ``name``; implied when exactly one is configured."""
        if not self.databases:
            raise SettingsError(self._no_config_message())
        if name is None:
            if len(self.databases) == 1:
                return next(iter(self.databases.values()))
            raise SettingsError(
                f"{self.config_path} configures several databases "
                f"({_names(self.databases)}); name one with --database <name> "
                f'or database("<name>")'
            )
        try:
            return self.databases[name]
        except KeyError:
            raise SettingsError(
                f"{self.config_path} configures no database `{name}`; "
                f"the configured databases are {_names(self.databases)}"
            ) from None

    def database_for(self, model: type) -> DatabaseSettings:
        """The database that owns ``model``, decided by its defining module.

        Refused when no database claims it, when several do (it then has no
        single owner; ask for a database by name), or when one of its foreign
        keys points at a model its database does not claim.
        """
        if not isinstance(model, type) or not hasattr(model, "__ferro_identity__"):
            raise SettingsError(
                f"{getattr(model, '__name__', model)!r} is not a ferro Model; "
                f"database_for() takes a Model subclass"
            )
        if not self.databases:
            raise SettingsError(self._no_config_message())
        owners = _owners(self.databases, model)
        if not owners:
            raise SettingsError(_unclaimed_message(self.databases, model))
        if len(owners) > 1:
            raise SettingsError(
                f"model {_identity(model)} is claimed by databases "
                f"{_names(owners)}, so it has no single owner; "
                f'choose one with database("<name>")'
            )
        _check_references(owners[0]._require_project(), model, owners[0])
        return owners[0]

    def _no_config_message(self) -> str:
        searched = "\n  ".join(str(path) for path in self.searched)
        return (
            f"no ferro config found; searched:\n  {searched}\n"
            f"Create a {FERRO_TOML} with models = [...] and dialects = [...], "
            f"or add a [tool.ferro] table to {PYPROJECT_TOML}, or point "
            f"{CONFIG_ENV} / --config at one"
        )


_LOCATED_FIELDS = ("config_path", "config_table", "searched")
"""The fields ``__init__`` sets from the lookup; never keys of the file."""


# -- lookup --------------------------------------------------------------------


def _locate(config: Path | str | None) -> tuple[Path | None, list[Path]]:
    """The config file to read, and every directory walked without finding one."""
    if config is not None:
        return _selected(Path(config), "config="), []
    from_env = os.environ.get(CONFIG_ENV)
    if from_env:
        return _selected(Path(from_env), CONFIG_ENV), []

    searched: list[Path] = []
    start = Path.cwd().resolve()
    for directory in (start, *start.parents):
        ferro_toml = directory / FERRO_TOML
        pyproject = directory / PYPROJECT_TOML
        has_ferro_toml = ferro_toml.is_file()
        has_pyproject = pyproject.is_file() and _config_table(pyproject) == _TOOL_FERRO
        if has_ferro_toml and has_pyproject:
            raise SettingsError(
                f"{directory} holds two ferro configs, {ferro_toml} and the "
                f"[tool.ferro] table of {pyproject}; ferro reads one and never "
                f"merges them. Remove one: delete {FERRO_TOML}, or remove "
                f"[tool.ferro] from {PYPROJECT_TOML}"
            )
        if has_ferro_toml:
            return ferro_toml, searched
        if has_pyproject:
            return pyproject, searched
        searched.append(directory)
    return None, searched


def _selected(path: Path, origin: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise SettingsError(
            f"{origin} names {resolved}, which does not exist; point it at a "
            f"{FERRO_TOML} or a {PYPROJECT_TOML} with a [tool.ferro] table"
        )
    return resolved


_TOOL_FERRO = ("tool", "ferro")


def _config_table(path: Path) -> tuple[str, ...]:
    """Where ``path`` keeps ferro's keys: ``("tool", "ferro")`` or the top level.

    The one look at the document before pydantic-settings reads it: the
    file's content, not its name, decides the table header, and a file
    carrying both shapes (or a ``[tool.ferro]`` that is not a table) is
    refused here because the source, once given a header, sees only that
    table.
    """
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path} is not valid TOML: {exc}; fix the file") from None
    tool = document.get("tool")
    if not (isinstance(tool, dict) and "ferro" in tool):
        return ()
    top_level_keys = [key for key in document if key in _FERRO_KEYS]
    if top_level_keys:
        keys = ", ".join(f"`{key}`" for key in top_level_keys)
        raise SettingsError(
            f"{path} carries ferro config twice: a [tool.ferro] table and "
            f"{keys} at the top level; ferro reads one and never merges them. "
            f"Keep one: move those keys into [tool.ferro], or remove [tool.ferro]"
        )
    if not isinstance(tool["ferro"], dict):
        raise SettingsError(
            f"{path}: [tool.ferro] must be a table with models = [...] and "
            f"dialects = [...]"
        )
    return _TOOL_FERRO


# -- shaping one file's keys ----------------------------------------------------


@dataclass(frozen=True)
class _Layout:
    """Where a config file keeps its keys, for refusals that say where a key goes."""

    path: Path
    header: tuple[str, ...]

    @property
    def top_label(self) -> str:
        return "[tool.ferro]" if self.header else f"the top level of {self.path.name}"

    def database_label(self, name: str) -> str:
        return f"[{_join(*self.header, 'databases')}.{name}]"

    def several(self, keys: dict[str, Any]) -> dict[str, tuple[str, Any]]:
        for key in keys:
            if key in _DATABASE_KEYS:
                raise SettingsError(
                    f"{self.path}: `{key}` sits at {self.top_label} beside a "
                    f"databases table; with several databases each one carries "
                    f"its own keys and nothing is inherited. Move `{key}` into "
                    f"{self.database_label('<name>')}"
                )
            self.refuse_unknown(key, self.top_label, allowed=_TOP_LEVEL_KEYS)
        databases = keys["databases"]
        if not isinstance(databases, dict) or not databases:
            raise SettingsError(
                f"{self.path}: `databases` must hold at least one "
                f"{self.database_label('<name>')} table with models and dialects"
            )
        return {
            name: (self.database_label(name), entry)
            for name, entry in databases.items()
        }

    def database(
        self, name: str, label: str, entry: Any, default_directory: Path
    ) -> dict[str, Any]:
        if not isinstance(entry, dict):
            raise SettingsError(
                f"{self.path}: {label} must be a table with models and dialects"
            )
        for key in entry:
            if key == "python_path":
                raise SettingsError(
                    f"{self.path}: `python_path` in {label} is a top-level key "
                    f"shared by every database; move it to {self.top_label}"
                )
            self.refuse_unknown(key, label, allowed=_DATABASE_KEYS)
        missing = [key for key in ("models", "dialects") if key not in entry]
        if missing:
            lines = "\n    ".join(_LINE_TO_ADD[key] for key in missing)
            raise SettingsError(
                f"{self.path}: {label} is incomplete; {_and(missing)} "
                f"{'has' if len(missing) == 1 else 'have'} no default. "
                f"Add to {label}:\n    {lines}"
            )
        directory = entry.get("directory")
        return {
            **entry,
            "name": name,
            "directory": (
                (self.path.parent / directory).resolve()
                if isinstance(directory, str)
                else (directory if directory is not None else default_directory)
            ),
        }

    def refuse_unknown(self, key: str, label: str, *, allowed: tuple[str, ...]) -> None:
        if key in allowed:
            return
        if key in _RESERVED_KEYS:
            raise SettingsError(
                f"{self.path}: `{key}` in {label} {_RESERVED_KEYS[key]}"
            )
        if key == "url":
            hint = (
                "; the URL is never in the file: set url_env to the name of the "
                "environment variable that holds it (DATABASE_URL by default), "
                "or pass --url"
            )
        elif key == "tool":
            hint = (
                "; ferro reads this file's top level because its [tool] table "
                "has no [tool.ferro]. Put ferro's keys under [tool.ferro], or "
                "remove [tool]"
            )
        else:
            hint = f"; the keys allowed there are {', '.join(allowed)}"
        raise SettingsError(f"{self.path}: unknown key `{key}` in {label}{hint}")


def _describe(layout: _Layout, exc: ValidationError) -> str:
    """Each pydantic error as ``<table> `<key>`: <problem>``."""
    several = _uses_databases_table(layout)
    lines = []
    for error in exc.errors():
        location = error["loc"]
        if location[:1] == ("databases",) and len(location) >= 2:
            name = str(location[1])
            where = layout.database_label(name) if several else layout.top_label
            key = _key_path(location[2:])
        else:
            where = layout.top_label
            key = _key_path(location)
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  {where}{f' `{key}`' if key else ''}: {message}")
    return f"{layout.path} is not a valid ferro config:\n" + "\n".join(lines)


def _uses_databases_table(layout: _Layout) -> bool:
    """Whether the file declares a ``databases`` table (only for labelling an
    error; the file already parsed once, so this read cannot fail)."""
    with layout.path.open("rb") as handle:
        table: Any = tomllib.load(handle)
    for key in layout.header:
        table = table[key]
    return "databases" in table


def _key_path(parts: tuple[int | str, ...]) -> str:
    """``("dialects", 1)`` -> ``dialects[1]``."""
    text = ""
    for part in parts:
        text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else part)
    return text


def _refuse_overlapping_directories(
    path: Path, databases: dict[str, DatabaseSettings]
) -> None:
    items = list(databases.values())
    for i, first in enumerate(items):
        for second in items[i + 1 :]:
            a, b = first.directory, second.directory
            if a == b or a in b.parents or b in a.parents:
                raise SettingsError(
                    f"{path}: databases `{first.name}` ({a}) and `{second.name}` "
                    f"({b}) have overlapping migration directories; each database "
                    f"is one lineage and needs a directory of its own. Set "
                    f"`directory` so neither is inside the other"
                )


# -- model ownership -----------------------------------------------------------


def _owners(
    databases: dict[str, DatabaseSettings], model: type
) -> list[DatabaseSettings]:
    """Every database claiming ``model`` by its defining module."""
    if len(databases) == 1:
        return list(databases.values())
    module = model.__module__
    return [
        database
        for database in databases.values()
        if any(_is_within(module, claimed) for claimed in database.models)
    ]


def _import_database_modules(project: _Project, database: DatabaseSettings) -> None:
    """Import ``database``'s ``models`` modules with the config directory,
    then ``python_path``, first on ``sys.path``."""
    roots = [project.config_path.parent, *project.python_path]
    _prepend_sys_path(roots)
    for module in database.models:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name is None or not _is_within(module, exc.name):
                raise
            searched = ", ".join(str(root) for root in roots)
            raise SettingsError(
                f"database `{database.name}` lists module `{module}` in models "
                f"({project.config_path}), but it cannot be imported: {exc}. "
                f"Searched the config directory and python_path first ({searched}). "
                f"If it lives under a source root, add that root to the top-level "
                f'python_path, e.g. python_path = ["src"]'
            ) from exc


def _models_of(project: _Project, database: DatabaseSettings) -> list[type]:
    from .registry import REGISTRY

    databases = project.databases
    registered = [m for m in REGISTRY.models().values() if isinstance(m, type)]
    mine = []
    for model in registered:
        owners = _owners(databases, model)
        if not owners:
            raise SettingsError(_unclaimed_message(databases, model))
        if any(owner.name == database.name for owner in owners):
            mine.append(model)
    for model in mine:
        _check_references(project, model, database)
    return mine


def _resolve_target(project: _Project, reference: str) -> type | None:
    """The model a string reference names, over the whole configured modelset.

    A database's own imports may not include the target (a string reference
    needs no import), so an unresolved reference imports every configured
    database's modules before deciding: the target may be another database's
    model, which is a cross-database foreign key, not a missing one.
    """
    from .registry import REGISTRY

    target = REGISTRY.resolve_reference(reference, default=None)
    if target is None:
        for database in project.databases.values():
            _import_database_modules(project, database)
        target = REGISTRY.resolve_reference(reference, default=None)
    return target


def _check_references(
    project: _Project, model: type, database: DatabaseSettings
) -> None:
    """Refuse a foreign key (or many-to-many) from ``model`` to a model that
    ``database`` does not claim."""
    databases = project.databases
    if len(databases) == 1:
        return

    relations = getattr(model, "ferro_relations", None) or {}
    for field_name, relation in relations.items():
        if not isinstance(relation, (ForeignKey, ManyToManyRelation)):
            continue
        target = relation.to
        if isinstance(target, (str, ForwardRef)):
            reference = target if isinstance(target, str) else target.__forward_arg__
            target = _resolve_target(project, reference)
            if target is None:
                raise SettingsError(
                    f"{_identity(model)}.{field_name} references `{reference}`, "
                    f"but no configured module defines it ({project.config_path}); "
                    f"add the module that defines `{reference}` to database "
                    f"`{database.name}`'s models"
                )
        target_owners = _owners(databases, target)
        if not target_owners:
            raise SettingsError(_unclaimed_message(databases, target))
        if not any(owner.name == database.name for owner in target_owners):
            raise SettingsError(
                f"{_identity(model)}.{field_name} in database `{database.name}` is a "
                f"foreign key to {_identity(target)} in database "
                f"{_names(target_owners)}; a foreign key cannot cross databases. "
                f"Put both models' modules in one database's models, or remove "
                f"the foreign key"
            )


def _unclaimed_message(databases: dict[str, DatabaseSettings], model: type) -> str:
    return (
        f"model {_identity(model)} is defined in module `{model.__module__}`, "
        f"which no database's models list holds (databases: {_names(databases)}); "
        f'add "{model.__module__}" (or a parent package) to the models key of '
        f"the database it belongs to"
    )


# -- small helpers -----------------------------------------------------------


def _prepend_sys_path(roots: list[Path]) -> None:
    entries = [str(root) for root in roots]
    sys.path[:] = entries + [entry for entry in sys.path if entry not in entries]
    importlib.invalidate_caches()


def _is_within(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _identity(model: type) -> str:
    return getattr(model, "__ferro_identity__", model.__qualname__)


def _names(databases: dict[str, DatabaseSettings] | list[DatabaseSettings]) -> str:
    names = databases if isinstance(databases, dict) else [d.name for d in databases]
    return ", ".join(f"`{name}`" for name in names)


def _and(words: list[str]) -> str:
    return " and ".join(words)


def _join(*parts: str) -> str:
    return ".".join(part for part in parts if part)


def _toml_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"
