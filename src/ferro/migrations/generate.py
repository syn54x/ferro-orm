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
import shutil
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._core import (
    _check_migrations,
    _generate_migration,
)
from . import scaffold
from .errors import MigrationRefused
from .scaffold import TEMPLATES_DIR
from .steps import StepRefused, scan_todos, unwritten
from .target import Target
from .layout import (
    SNAPSHOT_FILE,
    Change,
    GeneratedMigration,
    GeneratedStep,
    MigrationsDirectoryError,
    Moved,
    check_name,
    ensure_gitattributes,
    read_migrations,
    read_replacing,
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
    "replace",
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
    changes: tuple[Change, ...] = ()
    """An ``ungenerated`` problem's change summary, as data: what the
    migration ``ferro migrate new`` would write changes. Empty for every
    other kind."""
    headline: str | None = None
    """An ``ungenerated`` problem's message without the summary and notes it
    quotes (``the models changed since 0001_initial and no migration records
    it; run ...``), as the core builds it: what a terminal prints above the
    summary it lays out from :attr:`changes`. ``None`` for every other
    problem."""
    notes: tuple[str, ...] = ()
    """The notes an ``ungenerated`` problem's message quotes after its
    summary, as ``new`` prints them. Empty for every other problem."""


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
    method edit: ADR-0027) and no hand step (``sql_step``, ``data_step``)
    was asked for; a migration of hand steps alone stores the declared
    modelset as its snapshot. ``sql_step``
    appends a hand-written portable step (``NN_<sql_step>.up.sql`` /
    ``.down.sql``) holding ``-- write this step``. ``data_step`` (a model's
    class name, ``Author``) adds a Python data step
    ``NN_backfill_<model>.py`` whose ``up`` and ``down`` hold
    ``todo("write this step")`` (or the project's ``_templates/data_step.py``),
    after every generated step but the contract: a column or table the
    migration drops is dropped by the contract, after the data step, which
    can still read it (ADR-0025); with ``data_only`` the migration holds that
    step alone, whatever the models changed, and a full copy of its parent's
    snapshot. A change that
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


def replace(
    database: DatabaseSettings,
    name: str,
    target: str,
    *,
    sql_step: str | None = None,
    data_step: str | None = None,
    data_only: bool = False,
    no_backfill: Sequence[str] = (),
) -> Path:
    """Regenerate the migration ``target`` names as ``name`` on top of the
    newest schema snapshot below it (``ferro migrate new <name> --replace
    <target>``), and return its directory.

    Two branches each generated ``0007``; after the rebase the directory
    holds ``0007_add_x`` and ``0007_add_y``. ``replace(db, "add_x",
    "0007_add_x")`` writes ``0008_add_x`` chained from ``0007_add_y``'s
    snapshot and deletes ``0007_add_x``: a new snapshot, regenerated DDL
    steps, and every data step and hand-written SQL step carried across by
    name with its body intact, renumbered when the regenerated steps move
    it. A carried step wins over a rescaffolded step of its name. The number
    is kept when it is free. ``target`` is ``NNNN_<name>``, or ``NNNN`` when
    one migration carries the number. The other options mean what they mean
    to :func:`new`. It never opens a database (ADR-0023); a database that
    applied the old files meets the replaced migration as a changed one.

    Raises:
        MigrationsDirectoryError: the target is not in the directory, is a
            number two migrations share, or a later migration chains from it
            ("replace the later one first, or delete it"); the regeneration
            is empty and carries nothing ("delete the directory"); or what
            :func:`new` raises.
    """
    migration = prepare(
        database,
        name,
        sql_step=sql_step,
        data_step=data_step,
        data_only=data_only,
        no_backfill=no_backfill,
        replace=target,
    )
    if migration is None:  # pragma: no cover - prepare refuses an empty replace
        raise MigrationsDirectoryError(f"--replace {target}: nothing to write")
    for message in migration.warnings:
        warnings.warn(f"ferro migrate new: {message}", UserWarning, stacklevel=2)
    return write(database, migration)


def write(database: DatabaseSettings, migration: GeneratedMigration) -> Path:
    """Write a migration :func:`prepare` returned into ``database``'s
    directory (with its ``.gitattributes``); return the migration's directory.

    A replacement (``migration.replaces``) sets the replaced migration's
    directory aside under a hidden name naming both (``.0007_add_x.replaced``
    in place, ``.0007_add_x.0008_add_x.replaced`` renumbered), writes the new
    one, and deletes the aside; a write that raises puts it back. A run killed
    in between leaves the aside, which every directory reader refuses by
    name with its recovery (panel F6): rename it back while the replacement
    is absent, delete it once the replacement is in place."""
    directory = database.directory
    ensure_gitattributes(directory)
    if migration.replaces is None:
        return write_migration(directory, migration)
    old = directory / migration.replaces
    if migration.dir_name == migration.replaces:
        aside = directory / f".{migration.replaces}.replaced"
    else:
        aside = directory / f".{migration.replaces}.{migration.dir_name}.replaced"
    if aside.exists():
        raise MigrationsDirectoryError(
            f"{aside} exists: another `ferro migrate new --replace` is running, "
            "or one was interrupted. Nothing was written; `ferro migrate check` "
            "names how to recover it"
        )
    old.rename(aside)
    try:
        written = write_migration(directory, migration)
    except BaseException:
        aside.rename(old)
        raise
    shutil.rmtree(aside)
    return written


def prepare(
    database: DatabaseSettings,
    name: str,
    *,
    sql_step: str | None = None,
    data_step: str | None = None,
    data_only: bool = False,
    no_backfill: Sequence[str] = (),
    replace: str | None = None,
) -> GeneratedMigration | None:
    """The migration :func:`new` would write, without writing it; ``None``
    for no schema change. Raises what :func:`new` raises. With ``replace``,
    the migration :func:`replace` would write (never ``None``), raising what
    it raises."""
    check_name(name, "migration name")
    if sql_step is not None:
        check_name(sql_step, "--sql-step name")
    directory = database.directory
    replaced: dict[str, Any] | None = None
    carry: list[dict[str, Any]] = []
    if replace is None:
        migrations = read_migrations(directory)
    else:
        migrations, replaced = read_replacing(directory, replace)
        carry = _carried_steps(directory, replaced)
    parent = _head_snapshot_text(migrations)
    number = len(migrations) + 1
    try:
        # The generator plans once, decides the guard (ADR-0037), refusing a
        # --no-backfill it cannot honour, and lays out every step, the hand
        # steps too, whether or not the models changed: a --data-step before
        # the contract that drops what the step may read (ADR-0025), a
        # --sql-step right before it or last. --data-only is the data step
        # alone, with no schema plan.
        raw = _generate_migration(
            parent,
            json.dumps(declared_modelset(database)),
            list(database.dialects),
            options_json=json.dumps(
                {
                    "no_backfill": list(no_backfill),
                    "data_step": data_step,
                    "sql_step": sql_step,
                    "data_only": data_only,
                    "carry": [
                        {k: step[k] for k in ("name", "body", "kind", "down")}
                        for step in carry
                    ],
                }
            ),
        )
    except ValueError as err:
        raise MigrationsDirectoryError(str(err)) from None
    if raw is None and replaced is not None:
        old = _dir_name(replaced)
        below = _dir_name(migrations[-1]) if migrations else "no migration"
        raise MigrationsDirectoryError(
            f"--replace {replace}: on top of {below}, the models change nothing "
            f"that renders DDL, and {old} has no data step or hand-written SQL "
            f"step to carry, so the regenerated migration would be empty; delete "
            f"the directory {directory / old} instead. Nothing was written."
        )
    if raw is None:
        return None
    generated = json.loads(raw)
    migration = _with_data_steps(
        GeneratedMigration.from_generated(
            generated, number=number, name=name, carry=carry
        ),
        generated,
        directory / TEMPLATES_DIR,
    )
    if replaced is None:
        return migration
    return _with_moves(migration, replaced, carry, migrations)


def _dir_name(migration: dict[str, Any]) -> str:
    return f"{migration['number']:04d}_{migration['name']}"


def _carried_steps(directory: Path, replaced: dict[str, Any]) -> list[dict[str, Any]]:
    """Every data step and hand-written SQL step of ``replaced``, in order,
    as the generator's ``carry`` option takes them (plus each one's ``old``
    files, for the per-file lines). A dialect-suffixed SQL file is generated
    DDL: it is regenerated, never carried."""
    carry: list[dict[str, Any]] = []
    for step in replaced["steps"]:
        if step["kind"] not in ("data", "portable_sql"):
            continue
        files = step["files"]["portable"]
        up = Path(files["up"])
        down = Path(files["down"]) if files.get("down") else None
        carry.append(
            {
                "name": step["name"],
                "kind": step["kind"],
                "body": _text(directory, up),
                "down": _text(directory, down) if down is not None else None,
                "old": [p for p in (up, down) if p is not None],
            }
        )
    return carry


def _text(directory: Path, path: Path) -> str:
    """``path``'s text, exactly: a carried file is written back byte for byte."""
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise MigrationsDirectoryError(
            f"{path.relative_to(directory)} is not UTF-8, so --replace cannot carry "
            f"it byte for byte; save it as UTF-8 and run the command again. Nothing "
            f"was written."
        ) from None


def _with_moves(
    migration: GeneratedMigration,
    replaced: dict[str, Any],
    carry: list[dict[str, Any]],
    migrations: list[dict[str, Any]],
) -> GeneratedMigration:
    """``migration`` as the replacement of ``replaced``: what happened to
    each file the replaced migration had (kept, renumbered, carried). A
    carried step's old files are those of the ``carry`` entry the generator
    placed it from."""
    old_dir = _dir_name(replaced)
    old_files = {
        Path(path).name
        for step in replaced["steps"]
        for files in step["files"].values()
        for path in (files["up"], files.get("down"))
        if path
    }

    def unnumbered(file_name: str) -> str:
        return file_name[3:]

    by_rest = {unnumbered(name): name for name in old_files}
    moves: list[tuple[str, str, Moved]] = []
    for step in migration.steps:
        if step.carry is not None:
            new_files = sorted(step.files, key=lambda n: ".down." in n)
            for old, new in zip(carry[step.carry]["old"], new_files):
                moves.append((f"{old_dir}/{Path(old).name}", new, Moved.CARRIED))
            continue
        for new in step.files:
            if new in old_files:
                moves.append((f"{old_dir}/{new}", new, Moved.KEPT))
            elif unnumbered(new) in by_rest:
                old = by_rest[unnumbered(new)]
                moves.append((f"{old_dir}/{old}", new, Moved.RENUMBERED))
    return dataclass_replace(
        migration,
        replaces=old_dir,
        chained_from=_dir_name(migrations[-1]) if migrations else None,
        moves=tuple(moves),
    )


def _with_data_steps(
    migration: GeneratedMigration,
    generated: dict[str, Any],
    template_dir: Path,
) -> GeneratedMigration:
    """``migration`` with each data step's file written from the step record
    the generator produced (:func:`scaffold.render`), and each backfill that
    still needs writing named in a note after the generator's. The step's
    name and place are the generator's."""
    by_ordinal = {step["ordinal"]: step for step in generated["steps"]}
    steps: list[GeneratedStep] = []
    notes: list[str] = []
    for step in migration.steps:
        if step.carry is not None:
            steps.append(step)
            continue
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
                role=_data_role(record),
            )
        )
        note = scaffold.note(
            record, dir_name=migration.dir_name, migration_name=migration.name
        )
        if note is not None:
            notes.append(note)
    return dataclass_replace(
        migration, steps=tuple(steps), notes=(*migration.notes, *notes)
    )


def _data_role(record: Mapping[str, Any]) -> str:
    """The generator's word for a data step it laid out: ``data_step`` for
    the step ``--data-step`` asked for (its ``hand_model``), ``guard`` for
    the guard ``--no-backfill`` put in a backfill's place, ``backfill``
    otherwise."""
    if record.get("hand_model") is not None:
        return "data_step"
    return "guard" if record["data"]["guard"] else "backfill"


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
    problems = [
        Problem(
            p["kind"],
            p["message"],
            tuple(Change.from_json(c) for c in p["changes"]),
            p["headline"],
            tuple(p["notes"]),
        )
        for p in raw["problems"]
    ]
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
