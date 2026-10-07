"""``ferro migrate``: the sub-app every migration verb registers on.

This module defines ``init``. Later verbs register with ``@migrate.command``
and take ``glob: Annotated[Global, Parameter(parse=False)]`` to receive the
global options.

``init`` writes the project configuration :class:`~ferro.FerroSettings`
reads (ADR-0036, ADR-0039)::

    $ ferro migrate init
    Config file [pyproject.toml]:
    Models module (dotted) [myapp.models]:
    Target dialects [postgres]:
    Another database (separate tables, own migration history)? [y/N]:
    Wrote [tool.ferro] to pyproject.toml and created migrations/

Every prompt has a flag that answers it. The config is appended to an
existing ``pyproject.toml`` as text, so the user's other tables are never
reformatted.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

from ..settings import (
    DEFAULT_DATABASE,
    DEFAULT_DIRECTORY,
    FERRO_TOML,
    PYPROJECT_TOML,
    FerroSettings,
    SettingsError,
)
from . import Global, exit_codes

__all__ = ["migrate"]

migrate = App(
    name="migrate",
    help="Write, apply and check schema migrations.",
)

DIALECTS = ("postgres", "sqlite")
_DATABASE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_ANOTHER = "Another database (separate tables, own migration history)?"
_FIRST_NAME_DEFAULT = "main"


@dataclass(frozen=True)
class _Database:
    name: str
    models: list[str]
    dialects: list[str]
    directory: Path
    """Absolute."""


@migrate.command
def init(
    *,
    config_file: Annotated[
        Path | None,
        Parameter(
            help=(
                "The file to write: pyproject.toml (gets a [tool.ferro] table) "
                "or ferro.toml. Asked when omitted; the default is pyproject.toml "
                "when the project has one."
            )
        ),
    ] = None,
    models: Annotated[
        str | None,
        Parameter(help="The dotted module(s) defining the models, comma-separated."),
    ] = None,
    dialects: Annotated[
        str | None,
        Parameter(help="The target dialects, comma-separated: postgres, sqlite."),
    ] = None,
    directory: Annotated[
        Path | None,
        Parameter(
            help=(
                "Where migrations live (default migrations/; with several "
                "databases, one subdirectory each)."
            )
        ),
    ] = None,
    glob: Annotated[Global, Parameter(parse=False)],
) -> int:
    """Set a project up: write its ferro config and create its migrations directory.

    Asks for what the flags do not answer. --database names the one database
    configured from flags; interactively, answer "y" to "Another database?"
    to configure several.
    """
    if glob.url is not None:
        raise SettingsError(
            "init writes config and connects to no database; drop --url"
        )
    if glob.config is not None:
        raise SettingsError(
            "init writes a config file rather than reading one; name the file "
            "to write with --config-file"
        )

    cwd = Path.cwd().resolve()
    path = _config_path(cwd, config_file)
    in_pyproject = path.name == PYPROJECT_TOML
    _refuse_existing_config(path)

    root = (
        (cwd / directory).resolve()
        if directory is not None
        else path.parent / DEFAULT_DIRECTORY
    )
    _refuse_alembic(root, explicit=directory is not None)

    databases = _databases(path, root, models, dialects, glob.database)
    block = _render(databases, root, path, in_pyproject)
    _write(path, block)
    for database in databases:
        database.directory.mkdir(parents=True, exist_ok=True)

    print(block, end="")
    written = "[tool.ferro] to pyproject.toml" if in_pyproject else path.name
    created = " and ".join(f"{_shown(d.directory, cwd)}/" for d in databases)
    print(f"Wrote {written} and created {created}")
    return exit_codes.OK


@migrate.command
def new(
    name: Annotated[
        str,
        Parameter(help="The migration's name: lowercase letters, digits and _."),
    ],
    *,
    sql_step: Annotated[
        str | None,
        Parameter(
            help=(
                "Also add a hand-written SQL step NN_<name>.up.sql / .down.sql "
                "that serves every dialect."
            )
        ),
    ] = None,
    data_step: Annotated[
        str | None,
        Parameter(
            help=(
                "Also add a Python data step NN_backfill_<model>.py over this model "
                "(its class name), to be written where it says todo(...)."
            )
        ),
    ] = None,
    data_only: Annotated[
        bool,
        Parameter(
            help=(
                "Write only the --data-step: no DDL, and a full copy of the previous "
                "migration's snapshot."
            ),
            negative="",
        ),
    ] = False,
    glob: Annotated[Global, Parameter(parse=False)],
) -> int:
    """Write the next migration from the models' difference with the last one.

    Diffs the declared models against the newest migration's schema snapshot
    (never a database) and writes NNNN_<name>/ with one rendering per target
    dialect. A change that renders no DDL writes nothing, unless a step was
    asked for.
    """
    from ..migrations.generate import prepare, write

    _refuse_url(glob, "new")
    settings = FerroSettings(config=glob.config)
    database = settings.database(glob.database)
    migration = prepare(
        settings,
        database,
        name,
        sql_step=sql_step,
        data_step=data_step,
        data_only=data_only,
    )
    if migration is None:
        print("no schema change: nothing written")
        return exit_codes.OK
    written = write(database, migration)

    print(f"{_shown(written, Path.cwd().resolve())}/")
    for file_name in migration.files():
        print(f"  {file_name}")
    if migration.summary:
        print(migration.summary)
    for warning in migration.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return exit_codes.OK


@migrate.command
def check(*, glob: Annotated[Global, Parameter(parse=False)]) -> int:
    """Check, offline, that every model change has a migration and the
    migrations directory is intact.

    Exits 0 when it is, 3 naming each problem otherwise: an ungenerated
    model change, a broken snapshot chain, a duplicate or missing number, a
    step missing a target dialect's rendering, a data step still holding
    todo(...).
    """
    from ..migrations.generate import check as generate_check

    _refuse_url(glob, "check")
    settings = FerroSettings(config=glob.config)
    database = settings.database(glob.database)
    report = generate_check(settings, database)
    if report.ok:
        head = report.head or "no migration"
        print(f"ok: models match {head}")
        return exit_codes.OK
    for problem in report.problems:
        print(f"{problem.kind}: {problem.message}", file=sys.stderr)
    return exit_codes.PENDING


@migrate.command
def up(
    *,
    lock_timeout: Annotated[
        str,
        Parameter(
            help=(
                "How long to wait for another run's lock: 30s, 500ms, 1m, or a "
                "number of seconds (0 refuses at once)."
            )
        ),
    ] = "30s",
    glob: Annotated[Global, Parameter(parse=False)],
) -> int:
    """Apply every pending migration, in order, under the run lock.

    Prints one line per applied step. A refusal or a failed step prints why
    and how to go on, and exits 1; the next up resumes where this one stopped.
    """
    import asyncio

    from ..migrations.runner import up as run_up

    settings = FerroSettings(config=glob.config)
    database = settings.database(glob.database)
    report = asyncio.run(
        run_up(
            settings,
            database,
            url=glob.url,
            lock_timeout=lock_timeout,
            progress=lambda line: print(line, flush=True),
        )
    )
    if report.refusal is not None:
        print(report.refusal, file=sys.stderr)
        return exit_codes.REFUSED
    if not report.applied:
        print("nothing to apply: the database is up to date")
    return exit_codes.OK


_NOT_A_TERMINAL = (
    "Not a terminal: pass --yes to revert without a prompt. Nothing was reverted."
)


@migrate.command
def down(
    *,
    to: Annotated[
        str | None,
        Parameter(
            help=(
                "Where to stop: 0005 leaves 0005 fully applied, 0007:02 leaves "
                "steps 01-02 of 0007 applied, 0000 reverts everything. Without "
                "it, down reverts the latest applied migration."
            )
        ),
    ] = None,
    all_: Annotated[
        bool, Parameter(name="--all", negative="", help="Revert every migration.")
    ] = False,
    yes: Annotated[
        bool,
        Parameter(
            name=["--yes", "-y"], negative="", help="Revert without asking first."
        ),
    ] = False,
    lock_timeout: Annotated[
        str,
        Parameter(
            help=(
                "How long to wait for another run's lock: 30s, 500ms, 1m, or a "
                "number of seconds (0 refuses at once)."
            )
        ),
    ] = "30s",
    glob: Annotated[Global, Parameter(parse=False)],
) -> int:
    """Revert applied migrations, newest step first, under the run lock.

    Prints what it would revert and asks first (--yes skips the question).
    A step declared irreversible, or a migration a baseline recorded, stops
    the whole run before anything is reverted. A failed down prints why and
    exits 1, its step still recorded; the next down resumes there.
    """
    import asyncio

    from ..migrations.runner import DownPlan, plan_down
    from ..migrations.runner import down as run_down

    settings = FerroSettings(config=glob.config)
    database = settings.database(glob.database)
    if not yes and not sys.stdin.isatty():
        plan = asyncio.run(
            plan_down(settings, database, target=to, all=all_, url=glob.url)
        )
        if plan.refusal is not None:
            print(plan.refusal, file=sys.stderr)
            return exit_codes.REFUSED
        if not plan.steps:
            print("nothing to revert")
            return exit_codes.OK
        print(plan.describe())
        print(_NOT_A_TERMINAL, file=sys.stderr)
        return exit_codes.REFUSED

    def confirm(plan: DownPlan) -> bool:
        print(plan.describe(), flush=True)
        return yes or _confirm_revert(plan.question())

    report = asyncio.run(
        run_down(
            settings,
            database,
            target=to,
            all=all_,
            url=glob.url,
            lock_timeout=lock_timeout,
            confirm=confirm,
            progress=lambda line: print(line, flush=True),
        )
    )
    if report.refusal is not None:
        print(report.refusal, file=sys.stderr)
        return exit_codes.REFUSED
    if report.declined:
        print("Nothing was reverted.")
    elif not report.reverted:
        print("nothing to revert")
    return exit_codes.OK


def _confirm_revert(question: str) -> bool:
    """``Revert 0002_add_teams (1 step)? [y/N]``: anything but yes is no."""
    answer = _read(f"{question} [y/N] ", question, "--yes").lower()
    return answer in ("y", "yes")


@migrate.command
def status(
    *,
    steps: Annotated[
        bool,
        Parameter(
            help="Print every migration's steps, not only those needing attention."
        ),
    ] = False,
    json_: Annotated[
        bool, Parameter(name="--json", help="Print the report as a JSON document.")
    ] = False,
    glob: Annotated[Global, Parameter(parse=False)],
) -> int:
    """Show which migrations this database has applied, without changing it.

    Exits 0 when everything is installed, 3 when something is pending, 4 when
    something needs attention (a failed or interrupted step, an edited file,
    a database ahead of the directory).
    """
    import asyncio

    from ..migrations.runner import status as run_status

    settings = FerroSettings(config=glob.config)
    database = settings.database(glob.database)
    report = asyncio.run(run_status(settings, database, url=glob.url))
    print(report.to_json() if json_ else report.render(steps=steps))
    return report.exit_code


@migrate.command
def drift(*, glob: Annotated[Global, Parameter(parse=False)]) -> int:
    """Compare this database with the schema of the last migration applied to it.

    Prints one line per difference (a missing column, an invalid index, a
    check whose body changed, ...) and exits 4, or prints "no drift" and
    exits 0. Tables the migrations never declared are ignored. Exits 4 too,
    naming what to run, on a database mid-migration or with no migration
    records. Takes no lock and changes nothing.
    """
    import asyncio

    from ..migrations.drift import audit

    settings = FerroSettings(config=glob.config)
    database = settings.database(glob.database)
    report = asyncio.run(audit(database, url=glob.url))
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if report.refusal is not None:
        print(report.refusal, file=sys.stderr)
        return exit_codes.NEEDS_ATTENTION
    print(report.render())
    return exit_codes.OK if report.clean else exit_codes.NEEDS_ATTENTION


@migrate.command
def baseline(
    target: Annotated[
        str | None,
        Parameter(
            help=(
                "The last migration to record: 0006 or 0006_add_teams. Defaults "
                "to the newest migration."
            )
        ),
    ] = None,
    *,
    remove: Annotated[
        bool,
        Parameter(
            negative="",
            help=(
                "Delete the records a baseline wrote instead (refused while a "
                "migration a run applied stands above them)."
            ),
        ),
    ] = False,
    lock_timeout: Annotated[
        str,
        Parameter(
            help=(
                "How long to wait for another run's lock: 30s, 500ms, 1m, or a "
                "number of seconds (0 refuses at once)."
            )
        ),
    ] = "30s",
    glob: Annotated[Global, Parameter(parse=False)],
) -> int:
    """Record migrations as applied on a database that already has their schema.

    For a database auto-migrate or Alembic built before the project had
    migrations: checks it against the target migration's schema snapshot,
    as drift does, and records every step through the target (data steps
    included, listed, not run) only when nothing differs. Prints the
    differences and exits 4 otherwise, recording nothing; there is no flag
    to record past them. Refused (exit 1) when the database already has
    migration records.
    """
    import asyncio

    from ..migrations.baseline import record, render_removed
    from ..migrations.baseline import remove as remove_records

    settings = FerroSettings(config=glob.config)
    database = settings.database(glob.database)
    if remove:
        if target is not None:
            raise SettingsError(
                "baseline --remove removes the whole baseline; drop the target"
            )
        removed = asyncio.run(
            remove_records(database, url=glob.url, lock_timeout=lock_timeout)
        )
        print(render_removed(removed))
        return exit_codes.OK
    report = asyncio.run(
        record(database, target=target, url=glob.url, lock_timeout=lock_timeout)
    )
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(report.render())
    return exit_codes.OK if report.drift is None else exit_codes.NEEDS_ATTENTION


def _refuse_url(glob: Global, verb: str) -> None:
    if glob.url is not None:
        raise SettingsError(
            f"{verb} reads the models and the migrations directory, never a "
            f"database; drop --url"
        )


# -- which file ----------------------------------------------------------------


def _config_path(cwd: Path, flag: Path | None) -> Path:
    if flag is None:
        default = PYPROJECT_TOML if (cwd / PYPROJECT_TOML).is_file() else FERRO_TOML
        flag = Path(_ask("Config file", default=default, flag="--config-file"))
    path = (cwd / flag).resolve()
    if path.name not in (FERRO_TOML, PYPROJECT_TOML):
        raise SettingsError(
            f"init writes {FERRO_TOML} or {PYPROJECT_TOML}, the two files ferro's "
            f"config lookup finds; got {flag}. Pass --config-file {FERRO_TOML} "
            f"or --config-file {PYPROJECT_TOML}"
        )
    return path


def _refuse_existing_config(path: Path) -> None:
    """One config per project: refuse a second one in the file or beside it."""
    pyproject = path.parent / PYPROJECT_TOML
    ferro_toml = path.parent / FERRO_TOML
    if ferro_toml.is_file():
        raise SettingsError(
            f"{ferro_toml} already holds this project's ferro config; init "
            f"never rewrites one. Edit it, or delete it to start over"
        )
    if pyproject.is_file():
        try:
            document = tomllib.loads(pyproject.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise SettingsError(
                f"{pyproject} is not valid TOML: {exc}; fix the file"
            ) from None
        tool = document.get("tool")
        if isinstance(tool, dict) and "ferro" in tool:
            raise SettingsError(
                f"{pyproject} already has a [tool.ferro] table; init never "
                f"rewrites one. Edit it, or remove [tool.ferro] to start over"
            )


def _refuse_alembic(root: Path, *, explicit: bool) -> None:
    """The migrations directory must not be an Alembic environment's."""
    if not root.is_dir():
        return
    if (root / "env.py").is_file():
        found = "env.py"
    elif (root / "versions").is_dir():
        found = "versions"
    elif (root.parent / "alembic.ini").is_file():
        found = "alembic.ini beside it"
    else:
        return
    shown = _shown(root, Path.cwd().resolve())
    fix = (
        "Pass a different --directory"
        if explicit
        else "Pass --directory <path> to put ferro's migrations elsewhere "
        '(init writes directory = "<path>")'
    )
    raise SettingsError(
        f"{shown}/ holds an Alembic environment ({found}). {fix}, or move the "
        f"Alembic folder."
    )


# -- which databases -----------------------------------------------------------


def _databases(
    path: Path,
    root: Path,
    models_flag: str | None,
    dialects_flag: str | None,
    name_flag: str | None,
) -> list[_Database]:
    models = _models(path, models_flag, first=True)
    dialects = _dialects(dialects_flag, default="postgres")
    scripted = models_flag is not None or dialects_flag is not None
    if name_flag is not None:
        _check_name(name_flag, taken=set(), flag="--database")
    if scripted or not _confirm(_ANOTHER, flag="--models"):
        if name_flag is None or name_flag == DEFAULT_DATABASE:
            return [_Database(DEFAULT_DATABASE, models, dialects, root)]
        return [_Database(name_flag, models, dialects, root / name_flag)]

    first = name_flag or _ask_name(
        "Name of the first database", taken=set(), default=_FIRST_NAME_DEFAULT
    )
    databases = [_Database(first, models, dialects, root / first)]
    while True:
        name = _ask_name("Name of the next database", taken={d.name for d in databases})
        databases.append(
            _Database(
                name,
                _models(path, None, first=False),
                _dialects(None, default=",".join(databases[-1].dialects)),
                root / name,
            )
        )
        if not _confirm(_ANOTHER, flag="--models"):
            return databases


def _models(path: Path, flag: str | None, *, first: bool) -> list[str]:
    if flag is not None:
        models = _split(flag)
        if not models:
            raise SettingsError(
                "--models is empty; pass the dotted module(s), e.g. --models myapp.models"
            )
        return models
    default = _guess_models(path) if first else None
    while True:
        models = _split(
            _ask("Models module (dotted)", default=default, flag="--models")
        )
        if models:
            return models
        print(
            "Name the module(s) that define the models, e.g. myapp.models.",
            file=sys.stderr,
        )


def _dialects(flag: str | None, *, default: str) -> list[str]:
    if flag is not None:
        dialects = _split(flag)
        unknown = [d for d in dialects if d not in DIALECTS]
        if unknown or not dialects:
            raise SettingsError(
                f"--dialects {flag!r}: {_unknown_dialects(unknown)}; pass a "
                f"comma-separated list of {', '.join(DIALECTS)}"
            )
        return dialects
    while True:
        dialects = _split(_ask("Target dialects", default=default, flag="--dialects"))
        unknown = [d for d in dialects if d not in DIALECTS]
        if dialects and not unknown:
            return dialects
        print(
            f"{_unknown_dialects(unknown)}; answer with {', '.join(DIALECTS)}, "
            f"comma-separated.",
            file=sys.stderr,
        )


def _unknown_dialects(unknown: list[str]) -> str:
    if not unknown:
        return "no dialect given"
    return f"{', '.join(unknown)} {'is not a dialect' if len(unknown) == 1 else 'are not dialects'}"


def _ask_name(question: str, *, taken: set[str], default: str | None = None) -> str:
    while True:
        name = _ask(question, default=default, flag="--database")
        try:
            _check_name(name, taken=taken, flag="the name")
        except SettingsError as err:
            print(err, file=sys.stderr)
            continue
        return name


def _check_name(name: str, *, taken: set[str], flag: str) -> None:
    if not _DATABASE_NAME.match(name):
        raise SettingsError(
            f"{flag} {name!r} is not a database name; use letters, digits, _ and -"
        )
    if name in taken:
        raise SettingsError(
            f"database `{name}` is already configured; pick another name"
        )


def _guess_models(path: Path) -> str | None:
    """``<project name>.models`` from ``[project].name``, when there is one."""
    pyproject = path.parent / PYPROJECT_TOML
    if not pyproject.is_file():
        return None
    project = tomllib.loads(pyproject.read_text()).get("project")
    name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(name, str) or not name:
        return None
    return f"{re.sub(r'[^A-Za-z0-9_]', '_', name).lower()}.models"


# -- writing ---------------------------------------------------------------------


def _render(
    databases: list[_Database], root: Path, path: Path, in_pyproject: bool
) -> str:
    """The TOML block init appends, in exactly the shapes ``FerroSettings`` reads."""
    prefix = "tool.ferro" if in_pyproject else ""
    base = path.parent
    if len(databases) == 1 and databases[0].name == DEFAULT_DATABASE:
        database = databases[0]
        lines = [f"[{prefix}]"] if prefix else []
        lines += _keys(database)
        if database.directory != base / DEFAULT_DIRECTORY:
            lines.append(
                f"directory = {_toml_str(_relative(database.directory, base))}"
            )
        return "\n".join(lines) + "\n"

    blocks = []
    for database in databases:
        header = ".".join(part for part in (prefix, "databases", database.name) if part)
        lines = [f"[{header}]", *_keys(database)]
        lines.append(
            f"url_env = {_toml_str(database.name.upper().replace('-', '_') + '_DATABASE_URL')}"
        )
        if database.directory != base / DEFAULT_DIRECTORY / database.name:
            lines.append(
                f"directory = {_toml_str(_relative(database.directory, base))}"
            )
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks)


def _keys(database: _Database) -> list[str]:
    return [
        f"models = {_toml_list(database.models)}",
        f"dialects = {_toml_list(database.dialects)}",
    ]


def _write(path: Path, block: str) -> None:
    """Append ``block`` to ``path`` as text, after a blank line.

    The result is parsed before anything is written, so a file that would
    stop being valid TOML is refused and left as it was; once written, it is
    read back through ``FerroSettings``, and a refusal there restores the
    file to what it was before init ran.
    """
    existing = path.read_text() if path.is_file() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    text = f"{existing}\n{block}" if existing else block
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(
            f"appending ferro's config to {path} would make it invalid TOML "
            f"({exc}); add this to it by hand:\n{block}"
        ) from None
    original = path.read_bytes() if path.is_file() else None
    path.write_text(text)
    try:
        FerroSettings(config=path)  # what init wrote is what every consumer reads
    except SettingsError:
        if original is None:
            path.unlink()
        else:
            path.write_bytes(original)
        raise


# -- small helpers -----------------------------------------------------------------


def _ask(question: str, *, default: str | None, flag: str) -> str:
    """Ask one question on the terminal; an empty answer takes ``default``."""
    shown = f" [{default}]" if default is not None else ""
    answer = _read(f"{question}{shown}: ", question, flag)
    return answer or default or ""


def _confirm(question: str, *, flag: str) -> bool:
    """Ask a yes/no question whose default is no; ask again on anything else."""
    while True:
        answer = _read(f"{question} [y/N]: ", question, flag).lower()
        if answer in ("", "n", "no"):
            return False
        if answer in ("y", "yes"):
            return True
        print("Answer y or n.", file=sys.stderr)


def _read(prompt: str, question: str, flag: str) -> str:
    """Every prompt goes through here. With no terminal to ask on (end of
    input), refuse naming the flag that answers the question."""
    try:
        return input(prompt).strip()
    except EOFError:
        raise SettingsError(
            f"there is no terminal to ask {question!r} on; pass {flag}"
        ) from None


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _relative(target: Path, base: Path) -> str:
    return Path(os.path.relpath(target, base)).as_posix()


def _shown(target: Path, cwd: Path) -> str:
    return _relative(target, cwd)


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(value) for value in values) + "]"
