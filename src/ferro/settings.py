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
  and no ``.env`` is loaded; ``settings_customise_sources`` keeps only the
  values this module reads from the file. The database URL is never in the
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
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
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
)
_TOP_LEVEL_KEYS = ("python_path", "databases")
_RESERVED_KEYS = {
    "unmanaged_tables": (
        "is reserved for the list of live tables ferro leaves alone, which has "
        "not shipped yet (https://github.com/syn54x/ferro-orm/issues/486); "
        "remove it until #486 lands"
    ),
}
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
    """Parse a ``ddl_lock_timeout`` duration (``"500ms"``, ``"5s"``, ``"1m"``)."""
    match = _DURATION.match(value) if isinstance(value, str) else None
    if match is None or float(match.group(1)) <= 0:
        raise ValueError(
            f"ddl_lock_timeout must be a positive duration written as "
            f"{_DURATION_FORMS}; got {value!r}"
        )
    return timedelta(**{_DURATION_UNITS[match.group(2)]: float(match.group(1))})


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

    _project: _Project | None = PrivateAttr(default=None)

    @field_validator("ddl_lock_timeout")
    @classmethod
    def _parseable_duration(cls, value: str) -> str:
        parse_ddl_lock_timeout(value)
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
        """``ddl_lock_timeout`` in seconds."""
        return parse_ddl_lock_timeout(self.ddl_lock_timeout).total_seconds()

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
        ``python_path`` entry, and each module is imported as written. The
        result is every registered model this database claims, in
        registration order, after checking that every registered model has a
        database and that none of this database's models has a foreign key
        into another.
        """
        project = self._require_project()
        roots = [project.config_path.parent, *project.python_path]
        _prepend_sys_path(roots)
        for module in self.models:
            try:
                importlib.import_module(module)
            except ModuleNotFoundError as exc:
                if exc.name is None or not _is_within(module, exc.name):
                    raise
                searched = ", ".join(str(root) for root in roots)
                raise SettingsError(
                    f"database `{self.name}` lists module `{module}` in models "
                    f"({project.config_path}), but it cannot be imported: {exc}. "
                    f"Searched the config directory and python_path first ({searched}). "
                    f"If it lives under a source root, add that root to the top-level "
                    f'python_path, e.g. python_path = ["src"]'
                ) from exc
        return _models_of(project.databases, self)

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
    """

    model_config = SettingsConfigDict(extra="forbid", frozen=True)

    config_path: Path | None = None
    searched: list[Path] = Field(default_factory=list)
    python_path: list[Path] = Field(default_factory=list)
    databases: dict[str, DatabaseSettings] = Field(default_factory=dict)

    def __init__(self, config: Path | str | None = None) -> None:
        path, searched = _locate(config)
        if path is None:
            super().__init__(searched=searched)
            return
        config_file = _ConfigFile(path)
        values = config_file.values()
        try:
            super().__init__(config_path=path, **values)
        except ValidationError as exc:
            raise SettingsError(config_file.describe(exc)) from None
        project = _Project(path, tuple(self.python_path), self.databases)
        for database in self.databases.values():
            database._project = project
        _refuse_overlapping_directories(path, self.databases)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Only the values ``__init__`` read from the config file.

        No environment variable, ``.env`` file or secrets directory ever sets
        a field (ADR-0036: these keys decide what a migration contains).
        """
        return (init_settings,)

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
        _check_references(self.databases, model, owners[0])
        return owners[0]

    def _no_config_message(self) -> str:
        searched = "\n  ".join(str(path) for path in self.searched)
        return (
            f"no ferro config found; searched:\n  {searched}\n"
            f"Create a {FERRO_TOML} with models = [...] and dialects = [...], "
            f"or add a [tool.ferro] table to {PYPROJECT_TOML}, or point "
            f"{CONFIG_ENV} / --config at one"
        )


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
        has_pyproject = pyproject.is_file() and _has_tool_ferro(pyproject)
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


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path} is not valid TOML: {exc}; fix the file") from None


def _has_tool_ferro(pyproject: Path) -> bool:
    tool = _read_toml(pyproject).get("tool")
    return isinstance(tool, dict) and "ferro" in tool


# -- reading one file ----------------------------------------------------------


class _ConfigFile:
    """One config file's keys, checked and shaped for :class:`FerroSettings`.

    A file named ``pyproject.toml`` carries its keys under ``[tool.ferro]``;
    any other file (``ferro.toml``) carries them at the top level. Both read
    to the same values. Every key-level refusal (unknown, reserved,
    misplaced, missing) is decided here so its message can say where the key
    goes; type errors are left to pydantic and described by
    :func:`_describe_validation_error`.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.is_pyproject = path.name == PYPROJECT_TOML
        self.prefix = "tool.ferro" if self.is_pyproject else ""
        self.labels: dict[str, str] = {}

    @property
    def top_label(self) -> str:
        return (
            "[tool.ferro]"
            if self.is_pyproject
            else f"the top level of {self.path.name}"
        )

    def database_label(self, name: str) -> str:
        return f"[{_join(self.prefix, 'databases')}.{name}]"

    def values(self) -> dict[str, Any]:
        table = self._table()
        config_dir = self.path.parent
        python_path = table.get("python_path", [])
        values: dict[str, Any] = {
            "python_path": _resolve_paths(config_dir, python_path),
        }
        if "databases" in table:
            entries = self._several(table)
            defaults = {name: Path(DEFAULT_DIRECTORY, name) for name in entries}
        else:
            entries = {
                DEFAULT_DATABASE: (
                    self.top_label,
                    {k: v for k, v in table.items() if k != "python_path"},
                )
            }
            defaults = {DEFAULT_DATABASE: Path(DEFAULT_DIRECTORY)}
        self.labels = {name: label for name, (label, _) in entries.items()}
        values["databases"] = {
            name: self._database(name, label, entry, config_dir / defaults[name])
            for name, (label, entry) in entries.items()
        }
        return values

    def describe(self, exc: ValidationError) -> str:
        """Each pydantic error as ``<table> `<key>`: <problem>``."""
        lines = []
        for error in exc.errors():
            location = error["loc"]
            if location[:1] == ("databases",) and len(location) >= 2:
                where = self.labels.get(str(location[1]), self.top_label)
                key = _key_path(location[2:])
            else:
                where = self.top_label
                key = _key_path(location)
            message = error["msg"].removeprefix("Value error, ")
            lines.append(f"  {where}{f' `{key}`' if key else ''}: {message}")
        return f"{self.path} is not a valid ferro config:\n" + "\n".join(lines)

    def _table(self) -> dict[str, Any]:
        document = _read_toml(self.path)
        if not self.is_pyproject:
            if "tool" in document:
                raise SettingsError(
                    f"{self.path}: unknown key `tool`; a file not named "
                    f"{PYPROJECT_TOML} carries ferro's keys at the top level, "
                    f"without [tool.ferro]"
                )
            return document
        tool = document.get("tool")
        table = tool.get("ferro") if isinstance(tool, dict) else None
        if not isinstance(table, dict):
            raise SettingsError(
                f"{self.path} has no [tool.ferro] table; add one with models = "
                f"[...] and dialects = [...], or select a {FERRO_TOML} instead"
            )
        return table

    def _several(self, table: dict[str, Any]) -> dict[str, tuple[str, Any]]:
        for key in table:
            if key in _DATABASE_KEYS:
                raise SettingsError(
                    f"{self.path}: `{key}` sits at {self.top_label} beside a "
                    f"databases table; with several databases each one carries "
                    f"its own keys and nothing is inherited. Move `{key}` into "
                    f"{self.database_label('<name>')}"
                )
            self._refuse_unknown(key, self.top_label, allowed=_TOP_LEVEL_KEYS)
        databases = table["databases"]
        if not isinstance(databases, dict) or not databases:
            raise SettingsError(
                f"{self.path}: `databases` must hold at least one "
                f"{self.database_label('<name>')} table with models and dialects"
            )
        return {
            name: (self.database_label(name), entry)
            for name, entry in databases.items()
        }

    def _database(
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
            self._refuse_unknown(key, label, allowed=_DATABASE_KEYS)
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

    def _refuse_unknown(
        self, key: str, label: str, *, allowed: tuple[str, ...]
    ) -> None:
        if key in allowed:
            return
        if key in _RESERVED_KEYS:
            raise SettingsError(
                f"{self.path}: `{key}` in {label} {_RESERVED_KEYS[key]}"
            )
        hint = (
            "; the URL is never in the file: set url_env to the name of the "
            "environment variable that holds it (DATABASE_URL by default), "
            "or pass --url"
            if key == "url"
            else f"; the keys allowed there are {', '.join(allowed)}"
        )
        raise SettingsError(f"{self.path}: unknown key `{key}` in {label}{hint}")


def _resolve_paths(config_dir: Path, entries: Any) -> Any:
    """Resolve ``python_path`` against the config directory; leave a wrong
    type for pydantic to refuse by name."""
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        return entries
    return [(config_dir / entry).resolve() for entry in entries]


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


def _models_of(
    databases: dict[str, DatabaseSettings], database: DatabaseSettings
) -> list[type]:
    from .registry import REGISTRY

    registered = [m for m in REGISTRY.models().values() if isinstance(m, type)]
    mine = []
    for model in registered:
        owners = _owners(databases, model)
        if not owners:
            raise SettingsError(_unclaimed_message(databases, model))
        if any(owner.name == database.name for owner in owners):
            mine.append(model)
    for model in mine:
        _check_references(databases, model, database)
    return mine


def _check_references(
    databases: dict[str, DatabaseSettings], model: type, database: DatabaseSettings
) -> None:
    """Refuse a foreign key (or many-to-many) from ``model`` to a model that
    ``database`` does not claim."""
    if len(databases) == 1:
        return
    from .registry import REGISTRY

    relations = getattr(model, "ferro_relations", None) or {}
    for field_name, relation in relations.items():
        if not isinstance(relation, (ForeignKey, ManyToManyRelation)):
            continue
        target = relation.to
        if isinstance(target, (str, ForwardRef)):
            reference = target if isinstance(target, str) else target.__forward_arg__
            target = REGISTRY.resolve_reference(reference, default=None)
            if target is None:
                raise SettingsError(
                    f"{_identity(model)}.{field_name} references `{reference}`, "
                    f"which no imported module defines; add the module that "
                    f"defines it to database `{database.name}`'s models"
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
