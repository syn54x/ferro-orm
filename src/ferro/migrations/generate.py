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

import copy
import hashlib
import json
import shutil
import tempfile
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._core import (
    _check_migrations,
    _compose_run,
    _generate_migration,
)
from . import scaffold
from .errors import MigrationRefused
from .scaffold import TEMPLATES_DIR
from . import historical
from .historical import HistoricalModelError
from .steps import Chunked, StepRefused, load_step, scan_todos, unwritten
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
    read_run_reach,
    portable_sql_step,
    write_migration,
)

if TYPE_CHECKING:
    from ..settings import DatabaseSettings, FerroSettings
    from .context import HistoricalModels

__all__ = [
    "CheckReport",
    "MigrationsCheckError",
    "Problem",
    "check",
    "declared_modelset",
    "new",
    "prepare",
    "replace",
    "squash",
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


def squash(
    database: DatabaseSettings,
    name: str,
    from_: str,
    *,
    sql_step: str | None = None,
    data_step: str | None = None,
    data_only: bool = False,
    no_backfill: Sequence[str] = (),
) -> Path:
    """Replace the run from the migration ``from_`` names through the head
    with the one migration ``new`` would have written across it (``ferro
    migrate squash <name> --from <from_>``, ADR-0056), and return its
    directory.

    A feature branch wrote ``0005_add_teams`` through ``0009_fix_rows``;
    ``squash(db, "teams_epic", "0005")`` writes ``0005_teams_epic``, planned
    once by the one planner from the snapshot below the run (none for a run
    from ``0001``) to the run's composed target, with every data step and
    hand-written SQL step of the run carried byte for byte in chain order,
    and deletes the five. Its snapshot stores the composed target under the
    parent's checksum, with the witness naming each replaced migration.
    ``from_`` is ``NNNN_<name>``, or ``NNNN`` when one migration carries the
    number. The other options mean what they mean to :func:`new`. It reads
    snapshots and the directory only: the models are never imported, no
    database is opened.

    Raises:
        MigrationsDirectoryError: ``from_`` is not in the directory or is a
            number two migrations share; the run does not reach the head;
            the run's rename hints do not compose; the squash renders nothing
            and carries nothing ("delete the directories"); an aside of an
            earlier squash or replace is present; or what :func:`new` raises.
    """
    migration = prepare(
        database,
        name,
        sql_step=sql_step,
        data_step=data_step,
        data_only=data_only,
        no_backfill=no_backfill,
        squash_from=from_,
    )
    if migration is None:  # pragma: no cover - prepare refuses an empty squash
        raise MigrationsDirectoryError(f"squash --from {from_}: nothing to write")
    for message in migration.warnings:
        warnings.warn(f"ferro migrate squash: {message}", UserWarning, stacklevel=2)
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
    is absent, delete it once the replacement is in place.

    A squash moves the run's directories one by one into one aside named
    after both ends and the replacement
    (``.0005_add_teams..0009_fix_rows.0005_teams_epic.squashed``), writes the
    new migration, and deletes the aside; a write that raises moves them
    back. Every reader refuses a leftover aside the same way, naming any
    original still outside it."""
    directory = database.directory
    ensure_gitattributes(directory)
    if not migration.replaces:
        return write_migration(directory, migration)
    if migration.squash:
        return _write_squash(directory, migration)
    [replaced] = migration.replaces
    old = directory / replaced
    if migration.dir_name == replaced:
        aside = directory / f".{replaced}.replaced"
    else:
        aside = directory / f".{replaced}.{migration.dir_name}.replaced"
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


def _write_squash(directory: Path, migration: GeneratedMigration) -> Path:
    """:func:`write` for a squash: the originals into one aside, one by one,
    the replacement written, the aside deleted; a raise moves them back."""
    first, last = migration.replaces[0], migration.replaces[-1]
    aside = directory / f".{first}..{last}.{migration.dir_name}.squashed"
    if aside.exists():
        raise MigrationsDirectoryError(
            f"{aside} exists: another `ferro migrate squash` is running, or one was "
            "interrupted. Nothing was written; `ferro migrate check` names how to "
            "recover it"
        )
    aside.mkdir()
    moved: list[str] = []
    try:
        for name in migration.replaces:
            (directory / name).rename(aside / name)
            moved.append(name)
        written = write_migration(directory, migration)
    except BaseException:
        for name in reversed(moved):
            (aside / name).rename(directory / name)
        aside.rmdir()
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
    squash_from: str | None = None,
) -> GeneratedMigration | None:
    """The migration :func:`new` would write, without writing it; ``None``
    for no schema change. Raises what :func:`new` raises. With ``replace``,
    the migration :func:`replace` would write (never ``None``), raising what
    it raises. With ``squash_from``, the migration :func:`squash` would write
    (never ``None``), raising what it raises; the models are never read."""
    if replace is not None and squash_from is not None:
        raise ValueError("name either replace or squash_from, not both")
    check_name(name, "migration name")
    if sql_step is not None:
        check_name(sql_step, "--sql-step name")
    if squash_from is not None:
        return _prepare_squash(
            database,
            name,
            squash_from,
            {
                "no_backfill": list(no_backfill),
                "data_step": data_step,
                "sql_step": sql_step,
                "data_only": data_only,
            },
        )
    directory = database.directory
    replaced: dict[str, Any] | None = None
    carry: list[dict[str, Any]] = []
    if replace is None:
        migrations = read_migrations(directory)
    else:
        migrations, replaced = read_replacing(directory, replace)
        carry = _carried_steps(directory, [replaced])
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


def _prepare_squash(
    database: DatabaseSettings,
    name: str,
    from_: str,
    options: dict[str, Any],
) -> GeneratedMigration:
    """:func:`prepare` for :func:`squash`: the run read, its snapshots
    composed into the target (``_core._compose_run``), the migration planned
    once from the parent snapshot to that target through the generator ``new``
    uses, carrying the run's data steps and hand-written SQL steps and
    recording the witness."""
    directory = database.directory
    read = read_run_reach(directory, from_)
    below, run = read["below"], read["run"]
    names = [_dir_name(m) for m in run]
    if not read["reaches_head"]:
        raise MigrationsDirectoryError(_short_of_the_head(directory, names[0], read))
    written = f"{len(below) + 1:04d}_{name}"
    if written in names:
        raise MigrationsDirectoryError(
            f"ferro migrate squash: {name} would write {written}, the name of a "
            "migration it replaces, and every reader would then take the squash "
            "for the original it replaced; pick another name. Nothing was written."
        )
    carry = _carried_steps(directory, run)
    parent = _head_snapshot_text(below)
    try:
        composed = json.loads(
            _compose_run(
                parent,
                [_text(directory, Path(m["dir"]) / SNAPSHOT_FILE) for m in run],
            )
        )
    except ValueError as err:
        raise MigrationsDirectoryError(_compose_refusal(from_, err, names)) from None
    witness = [
        {
            "migration": _dir_name(m),
            "snapshot_checksum": m["snapshot"]["checksum"],
            "steps": [step["ordinal"] for step in m["steps"]],
            "replaces": m["replaces"],
        }
        for m in run
    ]
    try:
        raw = _generate_migration(
            parent,
            json.dumps(composed["target"]),
            list(database.dialects),
            options_json=json.dumps(
                {
                    **options,
                    "carry": [
                        {k: step[k] for k in ("name", "body", "kind", "down")}
                        for step in carry
                    ],
                    "replaces": witness,
                }
            ),
        )
    except ValueError as err:
        raise MigrationsDirectoryError(str(err)) from None
    if raw is None:
        raise MigrationsDirectoryError(
            _empty_squash(
                directory, from_, names, _dir_name(below[-1]) if below else None
            )
        )
    generated = json.loads(raw)
    migration = _with_data_steps(
        GeneratedMigration.from_generated(
            generated, number=len(below) + 1, name=name, carry=carry
        ),
        generated,
        directory / TEMPLATES_DIR,
    )
    squashed = _with_squash_moves(
        migration,
        carry,
        names,
        chained_from=_dir_name(below[-1]) if below else None,
        witness=witness,
        dead_names=composed["dead_names"],
    )
    return _with_dead_name_notes(squashed, carry, run, below, composed, names)


def _with_dead_name_notes(
    migration: GeneratedMigration,
    carry: list[dict[str, Any]],
    run: list[dict[str, Any]],
    below: list[dict[str, Any]],
    composed: dict[str, Any],
    names: list[str],
) -> GeneratedMigration:
    """``migration`` with a note for each dead name (a table, column, enum
    label or constraint that existed only inside the run, ADR-0056) no carried
    data step's declaration touches, or the refusal of the first carried step
    whose declaration does.

    A squash cannot read what a step's body does, but it can read what the
    step declares (ADR-0035): a chunked step's query, built over the
    historical models of the squashed migration widened by the dead names. The
    query's table, the columns its filter and order keys read and the enum
    labels its filter compares are names the step provably touches. A run
    that carries nothing has nothing to review, so it gets no note.

    Raises:
        MigrationsDirectoryError: the squashed migration's historical models
            cannot be built, or a carried data step does not load or its query
            does not build over them, or its declaration names a dead name.
    """
    dead = migration.dead_names
    touched: dict[str, str] = {}
    if any(entry["kind"] == "data" for entry in carry):
        models = _widened_models(run, below, composed, names)
        for entry in carry:
            if entry["kind"] != "data":
                continue
            for name in _declared_names(entry, models):
                touched.setdefault(name, _from_migration(entry["old"][0]))
    if not carry:
        return migration
    for item in dead:
        if item["name"] in touched:
            raise MigrationsDirectoryError(
                _dead_name_refusal(item, touched[item["name"]], names)
            )
    notes = [
        f"{item['name']} existed only inside the run "
        f"({names[item['appeared']]} through {names[item['vanished']]}): "
        f"review the carried steps for it"
        for item in dead
    ]
    return dataclass_replace(migration, notes=(*migration.notes, *notes))


def _dead_name_refusal(item: dict[str, Any], step: str, names: list[str]) -> str:
    """The refusal of a carried step whose declaration names a dead name."""
    removed = names[item["vanished"]]
    fix = f"start the range at {removed}, the migration that removed it (pass --from {removed})"
    return (
        f"ferro migrate squash: {step} names {item['name']}, which existed only "
        f"inside the run (added by {names[item['appeared']]}, removed by "
        f"{removed}): delete the step, or {fix}. Nothing was written."
    )


def _widened_models(
    run: list[dict[str, Any]],
    below: list[dict[str, Any]],
    composed: dict[str, Any],
    names: list[str],
) -> HistoricalModels:
    """The squashed migration's historical models (the snapshot below the run
    over the composed target, ADR-0025) plus every table and column any
    snapshot of the run held that the parent does not, so a declaration
    naming a dead name still builds and can be read.

    Raises:
        MigrationsDirectoryError: the squashed migration's historical models
            cannot be built (a column's storage changes inside the run), so a
            carried data step could not run against it.
    """
    parent = below[-1]["snapshot"]["ir"] if below else None
    held_by_parent = {
        m["table_name"]: {c["name"] for c in m["columns"]}
        for m in (parent["payload"]["models"] if parent else [])
    }
    try:
        historical.build(parent, composed["target"], rev=names[-1])
        widened = copy.deepcopy(composed["target"])
        tables = {m["table_name"]: m for m in widened["payload"]["models"]}
        for migration in run:
            for model in migration["snapshot"]["ir"]["payload"]["models"]:
                table = model["table_name"]
                held = tables.get(table)
                if held is None:
                    tables[table] = copy.deepcopy(model)
                    widened["payload"]["models"].append(tables[table])
                    continue
                have = {c["name"] for c in held["columns"]} | held_by_parent.get(
                    table, set()
                )
                held["columns"] += [
                    copy.deepcopy(c) for c in model["columns"] if c["name"] not in have
                ]
        return historical.build(parent, widened, rev=names[-1])
    except HistoricalModelError as err:
        raise MigrationsDirectoryError(f"{err} Nothing was written.") from None


def _declared_names(entry: dict[str, Any], models: HistoricalModels) -> set[str]:
    """The names a carried data step's declaration touches: for each
    ``@chunked`` shape of its ``up`` and ``down``, the query's table, the
    ``table.column`` of each column its filter and order keys read and the
    ``table.column:label`` of each value its filter compares.

    Raises:
        MigrationsDirectoryError: the step does not load, or a ``@chunked``
            query does not build over the squashed historical models widened
            by the dead names: the runner would refuse the squashed migration
            on it.
    """
    first = entry["old"][0]
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / first.parent.name / first.name
        path.parent.mkdir()
        path.write_text(entry["body"], encoding="utf-8")
        try:
            loaded = load_step(path, None)
        except StepRefused as refused:
            raise MigrationsDirectoryError(
                f"{refused}: {_from_migration(first)} cannot be carried as it is. "
                f"Fix the step in its migration, or delete it, and run the command "
                f"again. Nothing was written."
            ) from None
    touched: set[str] = set()
    for declared in (loaded.up, loaded.down):
        if not isinstance(declared.shape, Chunked):
            continue
        try:
            query = declared.shape.query(models)
        except Exception as err:
            raise MigrationsDirectoryError(
                f"ferro migrate squash: {_from_migration(first)}: @chunked's query "
                f"does not build over the squashed migration's models "
                f"({type(err).__name__}: {err}). Fix the step in its migration, or "
                f"delete it, and run the command again. Nothing was written."
            ) from None
        table = getattr(query.model_cls, "__ferro_table__", None)
        if table is None:
            continue
        touched.add(table)
        touched.update(f"{table}.{key.column}" for key in query.order_by_clause)
        for node in query.where_clause:
            _node_names(node, table, touched)
    return touched


def _node_names(node: Any, table: str, touched: set[str]) -> None:
    """Add the root-table columns (and compared enum labels) ``node`` reads."""
    for child in (node.left, node.right, node.child):
        if child is not None:
            _node_names(child, table, touched)
    if node.column is None or node.path:
        return
    column = f"{table}.{node.column}"
    touched.add(column)
    value = node.value
    for item in value if isinstance(value, list | tuple | set) else [value]:
        if isinstance(item, str | Enum):
            touched.add(f"{column}:{getattr(item, 'value', item)}")


def _short_of_the_head(directory: Path, first: str, read: dict[str, Any]) -> str:
    """The refusal of a run that stops short of the head: the first
    migration it leaves, what that one chains from, and the two fixes."""
    off = read["off_run"][0]
    # The reader leaves the off-run migrations unread: their links are read
    # here, as the stored bytes say, for the refusal's words only.
    checksums: dict[str, str] = {}
    links: dict[str, str | None] = {}
    for path in sorted(directory.glob(f"[0-9][0-9][0-9][0-9]_*/{SNAPSHOT_FILE}")):
        stored = path.read_bytes()
        checksums.setdefault(hashlib.sha384(stored).hexdigest(), path.parent.name)
        try:
            links[path.parent.name] = json.loads(stored).get("parent_checksum")
        except (ValueError, AttributeError):
            links[path.parent.name] = None

    def parent_of(name: str) -> str | None:
        link = links.get(name)
        return checksums.get(link) if link else None

    chains_from = parent_of(off)
    # The head's own ancestor at the run's number: where a squash reaching
    # the head can start.
    number = first[:4]
    head = max(read["off_run"])
    ancestor: str | None = head
    while ancestor is not None and ancestor[:4] > number:
        ancestor = parent_of(ancestor)
    start = (
        f"squash from a migration the head descends from (--from {ancestor}), or "
        if ancestor is not None and ancestor[:4] == number and ancestor != first
        else ""
    )
    return (
        f"the run from {first} does not reach the head: {off} chains from "
        f"{chains_from or 'no migration'}; {start}regenerate {first} on top of the "
        f"head with ferro migrate new <name> --replace {first}. Nothing was written."
    )


def _compose_refusal(from_: str, err: ValueError, names: list[str]) -> str:
    """A refusal of ``_compose_run``, its text kept as the core wrote it,
    then the ``--from`` a squash can pass instead, when there is one."""
    text = f"squash --from {from_}: {err}"
    at = getattr(err, "from_index", None)
    kind = getattr(err, "kind", None)
    if at is None or kind is None:
        return text
    if kind in ("uncomposable", "old_name_re_added"):
        return f"{text}; pass --from {names[at]} to end the range before it"
    return f"{text}; pass --from {names[at]} to start the range after {names[at - 1]}"


def _empty_squash(
    directory: Path, from_: str, names: list[str], below: str | None
) -> str:
    """The refusal of a squash that renders nothing and carries nothing."""
    run = names[0] if len(names) == 1 else f"{names[0]} through {names[-1]}"
    verb = "changes" if len(names) == 1 else "change"
    carry = "carries" if len(names) == 1 else "carry"
    paths = [str(directory / name) for name in names]
    if len(paths) == 1:
        delete = f"the directory {paths[0]}"
    else:
        delete = f"the directories {', '.join(paths[:-1])} and {paths[-1]}"
    return (
        f"squash --from {from_}: on top of {below or 'no migration'}, {run} {verb} "
        f"nothing that renders DDL and {carry} no data step or hand-written SQL "
        f"step, so the squashed migration would be empty; delete {delete} instead. "
        f"Nothing was written."
    )


def _with_squash_moves(
    migration: GeneratedMigration,
    carry: list[dict[str, Any]],
    names: list[str],
    *,
    chained_from: str | None,
    witness: list[dict[str, Any]],
    dead_names: list[dict[str, Any]],
) -> GeneratedMigration:
    """``migration`` as the squash of the run ``names``: each carried file
    and the original it was (``<migration>/<file>``), each generated DDL
    file regenerated, and a note for each carried data step still holding
    ``todo(...)``. A step asked for now (``--sql-step``, ``--data-step``)
    has no original: it is new."""
    moves: list[tuple[str, str, Moved]] = []
    notes: list[str] = []
    for step in migration.steps:
        if step.carry is not None:
            entry = carry[step.carry]
            new_files = sorted(step.files, key=lambda n: ".down." in n)
            for old, new in zip(entry["old"], new_files):
                moves.append((_from_migration(old), new, Moved.CARRIED))
            if step.kind == "data":
                notes += _still_to_write(step, entry)
            continue
        if step.kind == "ddl" or step.role in ("backfill", "guard"):
            moves += [(new, new, Moved.REGENERATED) for new in step.files]
    return dataclass_replace(
        migration,
        replaces=names,
        chained_from=chained_from,
        moves=tuple(moves),
        notes=(*migration.notes, *notes),
        witness=witness,
        dead_names=dead_names,
    )


def _from_migration(path: Path) -> str:
    """A step file as ``<migration>/<file>``."""
    return f"{path.parent.name}/{path.name}"


def _still_to_write(step: GeneratedStep, entry: dict[str, Any]) -> list[str]:
    """The note for a carried data step still holding ``todo(...)``; none
    for one written throughout, or one that does not parse (``check`` names
    that file)."""
    [name] = step.files
    try:
        todos = scan_todos(entry["body"], Path(name))
    except StepRefused:
        return []
    if not todos:
        return []
    return [
        f"{name} still needs writing where it says todo(...) (carried from "
        f"{_from_migration(entry['old'][0])})"
    ]


def _dir_name(migration: dict[str, Any]) -> str:
    return f"{migration['number']:04d}_{migration['name']}"


def _carried_steps(directory: Path, run: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every data step and hand-written SQL step of the ``run``'s migrations,
    in chain order and, within a migration, step order,
    as the generator's ``carry`` option takes them (plus each one's ``old``
    files, for the per-file lines). A dialect-suffixed SQL file is generated
    DDL: it is regenerated, never carried."""
    carry: list[dict[str, Any]] = []
    for migration in run:
        for step in migration["steps"]:
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
            f"{path.relative_to(directory)} is not UTF-8, so it cannot be carried "
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
        replaces=[old_dir],
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
