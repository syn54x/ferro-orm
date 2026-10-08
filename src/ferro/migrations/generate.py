"""``ferro migrate new`` and ``ferro migrate check``: the offline half.

A developer declares a model and asks for a migration::

    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    $ ferro migrate new create_author
    migrations/0001_create_author/
      01_schema.up.postgres.sql     CREATE TABLE "author" (...);
      01_schema.down.postgres.sql   -- ferro: destructive / DROP TABLE "author";
      ir.json

:func:`new` compiles the database's declared modelset, diffs it against the
head migration's schema snapshot through the one planner (in the Rust core,
which renders every statement), and writes the migration. It never opens a
database (ADR-0023). :func:`check` asks the same question without writing:
does every model change have a migration, and is the directory intact?
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._core import (
    _check_migrations,
    _generate_migration,
    _load_snapshot,
    _store_snapshot,
)
from . import scaffold
from .errors import MigrationRefused
from .scaffold import TEMPLATES_DIR
from .steps import StepRefused, scan_todos, unwritten
from .target import Target
from .layout import (
    SNAPSHOT_FILE,
    GeneratedMigration,
    GeneratedStep,
    MigrationsDirectoryError,
    check_name,
    ensure_gitattributes,
    read_migrations,
    portable_sql_step,
    write_migration,
)

if TYPE_CHECKING:
    from ..settings import DatabaseSettings, FerroSettings

__all__ = [
    "CheckReport",
    "MigrationsCheckError",
    "Problem",
    "check",
    "declared_modelset",
    "new",
    "prepare",
    "write",
]


class MigrationsCheckError(MigrationRefused):
    """Raised by :meth:`CheckReport.raise_for_problems`; carries the report."""

    def __init__(self, report: CheckReport) -> None:
        lines = "\n".join(f"  {p.kind}: {p.message}" for p in report.problems)
        super().__init__(f"ferro migrate check found problems:\n{lines}", report=report)


@dataclass(frozen=True)
class Problem:
    """One thing :func:`check` found wrong."""

    kind: str
    """A stable identifier: ``ungenerated``, ``broken_chain``,
    ``duplicate_number``, ``missing_number``, ``missing_rendering``, ..."""
    message: str
    """What is wrong and how to fix it."""


@dataclass(frozen=True)
class CheckReport:
    """What :func:`check` found."""

    ok: bool
    head: str | None = None
    """The newest migration's directory name, when there is one."""
    problems: list[Problem] = field(default_factory=list)

    def raise_for_problems(self) -> None:
        """Raise :class:`MigrationsCheckError` carrying this report when it
        has any problem; do nothing otherwise."""
        if self.problems:
            raise MigrationsCheckError(self)


def declared_modelset(database: DatabaseSettings) -> dict[str, Any]:
    """The SchemaIR modelset envelope ``database`` declares.

    Imports the database's ``models`` modules and keeps, from the resolved
    registry modelset, the models this database claims and the join tables
    between them (a join table rides its models).
    """
    from .. import ensure_resolved_modelset
    from ..registry import REGISTRY

    models = database.import_models()
    envelope = ensure_resolved_modelset()
    claimed = {id(model) for model in models}
    names = {key for key, model in REGISTRY.models().items() if id(model) in claimed}
    mine = [m for m in envelope["payload"]["models"] if m["model_name"] in names]
    tables = {m["table_name"] for m in mine}
    join_tables = REGISTRY.join_tables()
    for payload in envelope["payload"]["models"]:
        if payload["table_name"] in join_tables and all(
            fk["to_table"] in tables for fk in payload["foreign_keys"]
        ):
            mine.append(payload)
    return {**envelope, "payload": {**envelope["payload"], "models": mine}}


def _head_snapshot_text(migrations: list[dict[str, Any]]) -> str | None:
    """The head migration's ``ir.json`` text exactly as stored."""
    if not migrations:
        return None
    path = Path(migrations[-1]["dir"]) / SNAPSHOT_FILE
    return path.read_bytes().decode("utf-8")


def new(
    database: DatabaseSettings,
    name: str,
    *,
    sql_step: str | None = None,
    data_step: str | None = None,
    data_only: bool = False,
    no_backfill: Sequence[str] = (),
) -> Path | None:
    """Write the migration that brings ``database``'s migrations up to its models.

    Returns the new migration's directory, or ``None`` when the models
    change nothing that renders DDL (a Python default, a back-reference, a
    method edit: ADR-0027) and no ``sql_step`` was asked for. ``sql_step``
    appends a hand-written portable step (``NN_<sql_step>.up.sql`` /
    ``.down.sql``) holding ``-- write this step``. ``data_step`` (a model's
    class name, ``Author``) adds a Python data step
    ``NN_backfill_<model>.py`` whose ``up`` and ``down`` hold
    ``todo("write this step")`` (or the project's ``_templates/data_step.py``),
    after every generated step but the contract: a column or table the
    migration drops is dropped by the contract, after the data step, which
    can still read it (ADR-0025); with ``data_only`` the migration holds that step alone, whatever the
    models changed, and a full copy of its parent's snapshot. A change that
    asks existing rows for values gets a generated backfill per model
    (``NN_backfill_<model>.py``, ``todo`` where a value is missing);
    ``no_backfill`` (``["author.slug"]``) replaces a model's backfill with a
    generated guard step that fails the migration while any row still needs
    a value. Rendering warnings (a
    dialect that skips a declaration, such as row security on SQLite) are
    issued as ``UserWarning``.

    Raises:
        MigrationsDirectoryError: the directory is malformed, a name is not
            usable, or the models change something this generator does not
            generate yet (``not generated yet: <op> on <table> (ticket #N)``).
    """
    migration = prepare(
        database,
        name,
        sql_step=sql_step,
        data_step=data_step,
        data_only=data_only,
        no_backfill=no_backfill,
    )
    if migration is None:
        return None
    for message in migration.warnings:
        warnings.warn(f"ferro migrate new: {message}", UserWarning, stacklevel=2)
    return write(database, migration)


def write(database: DatabaseSettings, migration: GeneratedMigration) -> Path:
    """Write a migration :func:`prepare` returned into ``database``'s
    directory (with its ``.gitattributes``); return the migration's directory."""
    ensure_gitattributes(database.directory)
    return write_migration(database.directory, migration)


def prepare(
    database: DatabaseSettings,
    name: str,
    *,
    sql_step: str | None = None,
    data_step: str | None = None,
    data_only: bool = False,
    no_backfill: Sequence[str] = (),
) -> GeneratedMigration | None:
    """The migration :func:`new` would write, without writing it; ``None``
    for no schema change. Raises what :func:`new` raises."""
    check_name(name, "migration name")
    if sql_step is not None:
        check_name(sql_step, "--sql-step name")
    if data_only and no_backfill:
        raise MigrationsDirectoryError(
            "--no-backfill replaces a generated backfill, and --data-only generates none; "
            "drop one of them"
        )
    if data_only and (data_step is None or sql_step is not None):
        raise MigrationsDirectoryError(
            "--data-only writes one data step and nothing else; name its model with "
            "--data-step <Model> (and drop --sql-step)"
        )
    directory = database.directory
    migrations = read_migrations(directory)
    parent = _head_snapshot_text(migrations)
    number = len(migrations) + 1
    raw: str | None = None
    if not data_only:
        target = declared_modelset(database)
        try:
            # The generator decides the guard (ADR-0037), refusing a
            # --no-backfill it cannot honour, and places the hand steps: a
            # --data-step before the contract that drops what the step may
            # read (ADR-0025), a --sql-step right before it.
            raw = _generate_migration(
                parent,
                json.dumps(target),
                list(database.dialects),
                options_json=json.dumps(
                    {
                        "no_backfill": list(no_backfill),
                        "data_step": data_step,
                        "sql_step": sql_step,
                    }
                ),
            )
        except ValueError as err:
            raise MigrationsDirectoryError(str(err)) from None

    if raw is None:
        if sql_step is None and data_step is None:
            return None
        if parent is None:
            raise MigrationsDirectoryError(
                "a hand-written step needs a migration to follow; there is none"
            )
        # No schema change: a full copy of the parent's snapshot, and the hand
        # steps alone, the SQL step first.
        migration = GeneratedMigration(
            number=number, name=name, steps=(), snapshot_json=_store_snapshot(parent)
        )
        if sql_step is not None:
            migration = migration.with_sql_step(sql_step)
        if data_step is not None:
            model = _snapshot_model(migration.snapshot_json, data_step)
            # The record the generator writes for --data-step when models
            # changed (``hand_data_step``), placed last here.
            hand = {"name": f"backfill_{model.lower()}", "hand_model": model}
            migration = migration.with_data_step(
                hand["name"],
                scaffold.render(hand, template_dir=directory / TEMPLATES_DIR),
            )
    else:
        generated = json.loads(raw)
        migration = _with_data_steps(
            GeneratedMigration.from_generated(generated, number=number, name=name),
            generated,
            directory / TEMPLATES_DIR,
        )
    return migration


def _with_data_steps(
    migration: GeneratedMigration,
    generated: dict[str, Any],
    template_dir: Path,
) -> GeneratedMigration:
    """``migration`` with each data step's file written from the step record
    the generator produced (:func:`scaffold.render`), and each backfill that
    still needs writing named in the summary. The step's name and place are
    the generator's."""
    by_ordinal = {step["ordinal"]: step for step in generated["steps"]}
    steps: list[GeneratedStep] = []
    notes: list[str] = []
    for step in migration.steps:
        if step.kind == "portable_sql":
            # A --sql-step the generator placed: its placeholder files.
            steps.append(portable_sql_step(step.ordinal, step.name))
            continue
        record = by_ordinal[step.ordinal]
        if record.get("data") is None and record.get("hand_model") is None:
            steps.append(step)
            continue
        text = scaffold.render(record, template_dir=template_dir)
        steps.append(
            GeneratedStep(
                step.ordinal,
                step.name,
                "data",
                {f"{step.ordinal:02d}_{step.name}.py": text},
            )
        )
        note = scaffold.note(
            record, dir_name=migration.dir_name, migration_name=migration.name
        )
        if note is not None:
            notes.append(note)
    summary = "\n".join(line for line in (migration.summary, *notes) if line)
    return replace(migration, steps=tuple(steps), summary=summary)


def _snapshot_model(snapshot_json: str, name: str) -> str:
    """``name`` when the migration's snapshot has a model of that class name;
    refused, listing the models it has, otherwise."""
    models = json.loads(_load_snapshot(snapshot_json))["ir"]["payload"]["models"]
    names = sorted({str(m["model_name"]).rsplit(".", 1)[-1] for m in models})
    if name not in names:
        raise MigrationsDirectoryError(
            f"--data-step {name}: this migration's snapshot has no model {name!r}; "
            f"it has: {', '.join(names) or 'none'}"
        )
    return name


async def check(
    settings: FerroSettings | None = None, database: str | None = None
) -> CheckReport:
    """Check, offline, that every model change has a migration and the
    directory is intact (``ferro migrate check``), reading files only: it
    takes no connection.

    Reports an ungenerated model change (naming the models), a broken
    snapshot chain (naming both files), a duplicate or missing number, a
    DDL step without the rendering a target dialect needs, and a data step
    still holding ``todo(...)``. Raises nothing for a problem: call
    :meth:`CheckReport.raise_for_problems` to fail on one.

    Raises:
        MigrationRefused: the configuration names no single database.
    """
    chosen = Target.resolve(settings, database).database
    declared = declared_modelset(chosen)
    raw = json.loads(
        _check_migrations(
            str(chosen.directory), json.dumps(declared), list(chosen.dialects)
        )
    )
    problems = [Problem(p["kind"], p["message"]) for p in raw["problems"]]
    problems += _unwritten_steps(chosen.directory)
    return CheckReport(ok=not problems, head=raw["head"], problems=problems)


def _unwritten_steps(directory: Path) -> list[Problem]:
    """An ``unwritten_step`` problem for every ``todo("…")`` a data step
    still holds, read from its syntax tree (the file is never run)."""
    try:
        migrations = read_migrations(directory)
    except MigrationsDirectoryError:
        return []  # the core's check already reports a malformed directory
    problems: list[Problem] = []
    for migration in migrations:
        for step in migration["steps"]:
            if step["kind"] != "data":
                continue
            for file in step["files"].values():
                path = Path(file["up"])
                try:
                    todos = scan_todos(path.read_bytes(), path)
                except StepRefused as refused:
                    problems.append(Problem("unloadable_step", str(refused)))
                    continue
                problems += [
                    Problem("unwritten_step", line) for line in unwritten(path, todos)
                ]
    return problems
