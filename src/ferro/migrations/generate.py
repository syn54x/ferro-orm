"""`ferro migrate new` and `ferro migrate check`: the offline half.

A developer declares a model and asks for a migration:

```text
$ ferro migrate new create_author
migrations/0001_create_author/
  01_schema.up.postgres.sql     CREATE TABLE "author" (...);
  01_schema.down.postgres.sql   -- ferro: destructive / DROP TABLE "author";
  ir.json
```

`new` compiles the database's declared modelset, diffs it against the head
migration's schema snapshot through the planner (which renders every
statement), and writes the migration. It never opens a database. `check`
asks the same question without writing: does every model change have a
migration, and is the directory intact?
"""

from __future__ import annotations

import copy
import json
import shutil
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._core import (
    _check_migrations,
    _compose_range,
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
    read_range_reach,
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
    """The refusal `CheckReport.raise_for_problems()` raises; its `report` is the check's report."""

    def __init__(self, report: CheckReport) -> None:
        lines = "\n".join(f"  {p.kind}: {p.message}" for p in report.problems)
        super().__init__(f"ferro migrate check found problems:\n{lines}", report=report)


@dataclass(frozen=True)
class Problem:
    """One thing [`check`][ferro.migrations.check] found wrong."""

    kind: str
    """A stable identifier, such as `ungenerated`, `broken_chain`, `duplicate_number` or `unwritten_step`."""
    message: str
    """What is wrong and how to fix it."""
    changes: tuple[Change, ...] = ()
    """An `ungenerated` problem's change summary, as data; empty for every other kind.

    It says what the migration `ferro migrate new` would write changes.
    """
    headline: str | None = None
    """An `ungenerated` problem's message without the summary and notes it quotes; `None` otherwise.

    For example `the models changed since 0001_initial and no migration
    records it; run ...`: what a terminal prints above the summary it lays
    out from `changes`.
    """
    notes: tuple[str, ...] = ()
    """The notes an `ungenerated` problem's message quotes after its summary; empty otherwise."""


@dataclass(frozen=True)
class CheckReport:
    """What [`check`][ferro.migrations.check] found: whether the migrations are complete, and each problem.

    Examples:
        ```python {test=compile reason="needs a migrations project"}
        report = await ferro.migrations.check()
        for problem in report.problems:
            print(problem.kind, problem.message)
        report.raise_for_problems()
        ```
    """

    ok: bool
    """Whether `check` found no problem."""
    head: str | None = None
    """The newest migration's directory name, when there is one."""
    problems: list[Problem] = field(default_factory=list)
    """Every problem found, in the order found."""

    def raise_for_problems(self) -> None:
        """Raise when the report has any problem; do nothing otherwise.

        Raises:
            MigrationsCheckError: The report has a problem; the message
                lists each, and its `report` is this report.
        """
        if self.problems:
            raise MigrationsCheckError(self)


def declared_modelset(database: DatabaseSettings) -> dict[str, Any]:
    """Compile the schema a configured database's models declare.

    Imports the database's `models` modules and keeps, from the resolved
    registry modelset, the models this database claims and the join tables
    between them (a join table rides its models).

    Args:
        database: The configured database.

    Returns:
        The modelset, in the form a migration's `ir.json` stores.
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
    """Write the migration that brings a database's migrations up to its models.

    This is `ferro migrate new`. A change that asks existing rows for values
    gets a generated backfill per model (`NN_backfill_<model>.py`, with
    `todo` where a value is missing). A migration of hand steps alone stores
    the declared modelset as its snapshot. Rendering warnings (a dialect
    that skips a declaration, such as row security on SQLite) are issued as
    `UserWarning`.

    Args:
        database: The configured database.
        name: The migration's name: lowercase letters, digits and `_`.
        sql_step: Also add a hand-written portable SQL step
            `NN_<sql_step>.up.sql` / `.down.sql` holding `-- write this step`.
        data_step: Also add a Python data step `NN_backfill_<model>.py` over
            this model (its class name, `Author`), whose `up` and `down` hold
            `todo("write this step")` (or the project's
            `_templates/data_step.py`). It lands after every generated step
            but the contract, so it can still read what the contract drops.
        data_only: Write only the `data_step`: no DDL, and a full copy of the
            parent's snapshot.
        no_backfill: Columns (`"author.slug"`) whose generated backfill is
            replaced by a guard step that fails the migration while any row
            still needs a value.

    Returns:
        The new migration's directory, or `None` when the models change
            nothing that renders DDL (a Python default, a back-reference, a
            method edit) and no hand step was asked for.

    Raises:
        MigrationsDirectoryError: The directory is malformed, a name is not
            usable, or the models change something the generator does not
            generate yet.
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
    """Regenerate a migration on top of the newest schema snapshot below it.

    This is `ferro migrate new <name> --replace <target>`. Two branches each
    generated `0007`; after the rebase the directory holds `0007_add_x` and
    `0007_add_y`. `replace(db, "add_x", "0007_add_x")` writes `0008_add_x`
    chained from `0007_add_y`'s snapshot and deletes `0007_add_x`: a new
    snapshot, regenerated DDL steps, and every data step and hand-written
    SQL step carried across by name with its body intact, renumbered when
    the regenerated steps move it. A carried step wins over a rescaffolded
    step of its name. The number is kept when it is free. It never opens a
    database; a database that applied the old files meets the replaced
    migration as a changed one.

    Args:
        database: The configured database.
        name: The regenerated migration's name.
        target: The migration to regenerate, `NNNN_<name>`, or `NNNN` when
            one migration carries the number.
        sql_step: As for `new`.
        data_step: As for `new`.
        data_only: As for `new`.
        no_backfill: As for `new`.

    Returns:
        The regenerated migration's directory.

    Raises:
        MigrationsDirectoryError: The target is not in the directory, is a
            number two migrations share, or a later migration chains from it;
            the regeneration is empty and carries nothing; or what `new`
            raises.
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
    """Replace a range of migrations, through the head, with the one migration `new` would have written.

    This is `ferro migrate squash <name> --from <from_>`. A feature branch
    wrote `0005_add_teams` through `0009_fix_rows`;
    `squash(db, "teams_epic", "0005")` writes `0005_teams_epic`, planned
    once from the snapshot below the range (none for a range from `0001`) to
    the range's composed target, with every data step and hand-written SQL
    step of the range carried byte for byte in chain order, and deletes the
    five. Its snapshot stores the composed target under the parent's
    checksum, with a witness naming each replaced migration. It reads
    snapshots and the directory only: the models are never imported, no
    database is opened.

    Args:
        database: The configured database.
        name: The squashed migration's name.
        from_: The first migration of the range, `NNNN_<name>`, or `NNNN`
            when one migration carries the number.
        sql_step: As for `new`.
        data_step: As for `new`.
        data_only: As for `new`, with a full copy of the snapshot below the
            range.
        no_backfill: As for `new`.

    Returns:
        The squashed migration's directory.

    Raises:
        MigrationsDirectoryError: `from_` is not in the directory or is a
            number two migrations share; the range does not reach the head;
            the range's rename hints do not compose; the squash renders
            nothing and carries nothing; an aside of an earlier squash or
            replace is present; or what `new` raises.
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
    """Write a prepared migration into a database's migrations directory.

    A replacement sets the replaced migration's directory aside under a
    hidden name naming both (`.0007_add_x.replaced` in place,
    `.0007_add_x.0008_add_x.replaced` renumbered), writes the new one, and
    deletes the aside; a write that raises puts it back. A run killed in
    between leaves the aside, which every directory reader refuses by name
    with its recovery: rename it back while the replacement is absent,
    delete it once the replacement is in place. A squash moves the range's
    directories one by one into one aside named after both ends and the
    replacement (`.0005_add_teams..0009_fix_rows.0005_teams_epic.squashed`)
    and recovers the same way.

    Args:
        database: The configured database.
        migration: The migration `prepare` returned.

    Returns:
        The written migration's directory.

    Raises:
        MigrationsDirectoryError: An aside of another replace or squash is
            present.
    """
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
    """`write` for a squash: the originals into one aside, one by one,
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
    """Build the migration `new`, `replace` or `squash` would write, without writing it.

    Args:
        database: The configured database.
        name: The migration's name.
        sql_step: As for `new`.
        data_step: As for `new`.
        data_only: As for `new`.
        no_backfill: As for `new`.
        replace: Build what `replace` would write for this target.
        squash_from: Build what `squash` would write from this migration;
            the models are never read.

    Returns:
        The migration, or `None` for no schema change (never `None` with
            `replace` or `squash_from`).

    Raises:
        ValueError: Both `replace` and `squash_from` are given.
        MigrationsDirectoryError: What `new`, `replace` or `squash` raises.
    """
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
    written = f"{number:04d}_{name}"
    squash_of = _witnessed(migrations).get(written)
    if squash_of is not None:
        raise MigrationsDirectoryError(_witnessed_name("new", written, squash_of))
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
    """`prepare` for `squash`: the range read, its snapshots
    composed into the target (``_core._compose_range``), the migration planned
    once from the parent snapshot to that target through the generator ``new``
    uses, carrying the range's data steps and hand-written SQL steps and
    recording the witness."""
    directory = database.directory
    read = read_range_reach(directory, from_)
    below, range_ = read["below"], read["range"]
    names = [_dir_name(m) for m in range_]
    if read["off_range"]:
        raise MigrationsDirectoryError(_short_of_the_head(names[0], read["off_range"]))
    if read["siblings"]:
        raise MigrationsDirectoryError(_shared_number(names[0], read["siblings"][0]))
    written = f"{len(below) + 1:04d}_{name}"
    if written in names:
        raise MigrationsDirectoryError(
            f"ferro migrate squash: {name} would write {written}, the name of a "
            "migration it replaces, and every reader would then take the squash "
            "for the original it replaced; pick another name. Nothing was written."
        )
    squash_of = _witnessed(below + range_).get(written)
    if squash_of is not None:
        raise MigrationsDirectoryError(_witnessed_name("squash", written, squash_of))
    carry = _carried_steps(directory, range_)
    parent = _head_snapshot_text(below)
    try:
        composed = json.loads(
            _compose_range(
                parent,
                [_text(directory, Path(m["dir"]) / SNAPSHOT_FILE) for m in range_],
            )
        )
    except ValueError as err:
        raise MigrationsDirectoryError(_compose_refusal(from_, err, names)) from None
    if options["data_only"] and options["data_step"] is not None:
        _refuse_data_only_over_ddl(
            parent, composed["target"], list(database.dialects), names, name, options
        )
    witness = [
        {
            "migration": _dir_name(m),
            "snapshot_checksum": m["snapshot"]["checksum"],
            "steps": [step["ordinal"] for step in m["steps"]],
            "replaces": m["replaces"],
        }
        for m in range_
    ]

    def generate(carry: list[dict[str, Any]]) -> GeneratedMigration:
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
        return _with_data_steps(
            GeneratedMigration.from_generated(
                generated, number=len(below) + 1, name=name, carry=carry
            ),
            generated,
            directory / TEMPLATES_DIR,
        )

    migration = generate(carry)
    dropped = _unneeded_scaffolds(
        migration, carry, range_, below, directory, list(database.dialects)
    )
    if dropped:
        # Regenerated without them, so the steps after renumber densely.
        carry = [entry for i, entry in enumerate(carry) if i not in dropped]
        migration = generate(carry)
        migration = dataclass_replace(
            migration, notes=(*migration.notes, *dropped.values())
        )
    squashed = _with_squash_moves(
        migration,
        carry,
        names,
        chained_from=_dir_name(below[-1]) if below else None,
        witness=witness,
        dead_names=composed["dead_names"],
    )
    return _with_dead_name_notes(squashed, carry, range_, below, composed, names)


def _unneeded_scaffolds(
    migration: GeneratedMigration,
    carry: list[dict[str, Any]],
    range_: list[dict[str, Any]],
    below: list[dict[str, Any]],
    directory: Path,
    dialects: list[str],
) -> dict[int, str]:
    """The carried data steps a squash drops, by their place in ``carry``,
    each with the note that names it.

    A carried data step is dropped when it is still, byte for byte, the
    backfill the generator scaffolded for its original migration (that
    original regenerated alone, from its own parent snapshot to its own
    snapshot, through the path ``new`` takes) and the squashed ``migration``
    lays out no step of its name: a table the squash creates has no rows to
    backfill. A step with any byte a person wrote is carried; so is one the
    squashed plan rescaffolds, which it wins over as before. An original the
    generator cannot regenerate alone (one planned with an option its
    snapshots do not record) has no scaffold to compare, so its steps are
    carried."""
    candidates = sorted(
        step.carry
        for step in migration.steps
        if step.carry is not None and step.kind == "data" and not step.over_namesake
    )
    at_of = {_dir_name(m): at for at, m in enumerate(range_)}
    scaffolds: dict[int, dict[str, tuple[str, str]]] = {}
    dropped: dict[int, str] = {}
    for index in candidates:
        entry = carry[index]
        original = Path(entry["old"][0])
        at = at_of[original.parent.name]
        if at not in scaffolds:
            scaffolds[at] = _original_backfills(range_, below, at, directory, dialects)
        scaffold = scaffolds[at].get(entry["name"])
        if scaffold is None or scaffold[0] != entry["body"]:
            continue
        dropped[index] = (
            f"{original.name} from {original.parent.name} was the generator's "
            f"unwritten scaffold and the squashed plan needs no backfill of "
            f"{scaffold[1]}: dropped"
        )
    return dropped


def _original_backfills(
    range_: list[dict[str, Any]],
    below: list[dict[str, Any]],
    at: int,
    directory: Path,
    dialects: list[str],
) -> dict[str, tuple[str, str]]:
    """The backfills the generator scaffolds for the range's migration ``at``
    alone, by step name: the scaffold's text, as ``new`` writes it, and the
    columns it fills (``author.bio``)."""
    try:
        raw = _generate_migration(
            _head_snapshot_text(range_[:at] or below),
            json.dumps(range_[at]["snapshot"]["ir"]),
            dialects,
        )
    except ValueError:
        return {}
    if raw is None:
        return {}
    generated = json.loads(raw)
    regenerated = _with_data_steps(
        GeneratedMigration.from_generated(
            generated, number=range_[at]["number"], name=range_[at]["name"]
        ),
        generated,
        directory / TEMPLATES_DIR,
    )
    records = {step["ordinal"]: step for step in generated["steps"]}
    backfills: dict[str, tuple[str, str]] = {}
    for step in regenerated.steps:
        if step.role != "backfill":
            continue
        data = records[step.ordinal]["data"]
        columns = dict.fromkeys(column["name"] for column in data["columns"])
        [text] = step.files.values()
        backfills[step.name] = (
            text,
            ", ".join(f"{data['table']}.{column}" for column in columns),
        )
    return backfills


def _with_dead_name_notes(
    migration: GeneratedMigration,
    carry: list[dict[str, Any]],
    range_: list[dict[str, Any]],
    below: list[dict[str, Any]],
    composed: dict[str, Any],
    names: list[str],
) -> GeneratedMigration:
    """``migration`` with a note for each dead name (a table, column, enum
    label or constraint that existed only inside the range) no carried
    data step's declaration touches, or the refusal of the first carried step
    whose declaration does.

    A squash cannot read what a step's body does, but it can read what the
    step declares: a chunked step's query, built over the
    historical models of the squashed migration widened by the dead names. The
    query's table, the columns its filter and order keys read and the enum
    labels its filter compares are names the step provably touches. A range
    that carries nothing has nothing to review, so it gets no note.

    Raises:
        MigrationsDirectoryError: the squashed migration's historical models
            cannot be built, or a carried data step does not load or its query
            does not build over them, or its declaration names a dead name.
    """
    dead = migration.dead_names
    touched: dict[str, str] = {}
    if any(entry["kind"] == "data" for entry in carry):
        models = _widened_models(range_, below, composed, names)
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
        f"{item['name']} existed only inside the range "
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
        f"inside the range (added by {names[item['appeared']]}, removed by "
        f"{removed}): delete the step, or {fix}. Nothing was written."
    )


def _widened_models(
    range_: list[dict[str, Any]],
    below: list[dict[str, Any]],
    composed: dict[str, Any],
    names: list[str],
) -> HistoricalModels:
    """The squashed migration's historical models (the snapshot below the range
    over the composed target) plus every table and column any
    snapshot of the range held that the parent does not, so a declaration
    naming a dead name still builds and can be read.

    Raises:
        MigrationsDirectoryError: the squashed migration's historical models
            cannot be built (a column's storage changes inside the range), so a
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
        for migration in range_:
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
    try:
        # The original itself, whose bytes are the carried body: every
        # refusal of it names it as ``<migration>/<file>``.
        loaded = load_step(first, None)
    except StepRefused as refused:
        reason = str(refused).removeprefix("ferro migrate: ")
        raise MigrationsDirectoryError(
            f"ferro migrate squash: {reason}; the squash cannot carry it as it is. "
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


def _shared_number(first: str, sibling: str) -> str:
    """The refusal of a range whose first number another migration shares: no
    range descends from that sibling, so a squash planned on top of it would
    drop what it made. The fix is ``new --replace`` on either one."""
    return (
        f"ferro migrate squash: {sibling} shares {first}'s number and is not in "
        f"the range: two branches each generated {first[:4]}. Regenerate one on "
        f"top of the other first (ferro migrate new {sibling[5:]} --replace "
        f"{sibling}, or ferro migrate new {first[5:]} --replace {first}), then "
        f"squash. Nothing was written."
    )


def _short_of_the_head(first: str, off_range: list[dict[str, Any]]) -> str:
    """The refusal of a range that stops short of the head: the first
    migration it leaves, what that one chains from (the core's read of each
    off-range migration's link), and the two fixes."""
    chains_from = {off["migration"]: off["chains_from"] for off in off_range}
    off = off_range[0]["migration"]
    # The head's own ancestor at the range's number: where a squash reaching
    # the head can start. Above that number every ancestor off the range is
    # an off-range migration; one on the range means the head descends from
    # ``first`` itself.
    number = first[:4]
    ancestor: str | None = max(chains_from)
    while ancestor is not None and ancestor[:4] > number:
        ancestor = chains_from.get(ancestor)
    start = (
        f"squash from a migration the head descends from (--from {ancestor}), or "
        if ancestor is not None and ancestor[:4] == number and ancestor != first
        else ""
    )
    return (
        f"the range from {first} does not reach the head: {off} chains from "
        f"{chains_from[off] or 'no migration'}; {start}regenerate {first} on top of "
        f"the head with ferro migrate new {first[5:]} --replace {first}. Nothing was "
        f"written."
    )


def _witnessed(migrations: list[dict[str, Any]]) -> dict[str, str]:
    """Every migration name a squash's witness among ``migrations`` records,
    at any depth (``NNNN_<name>``), and the squash that replaced it."""
    named: dict[str, str] = {}

    def walk(squash: str, witness: list[dict[str, Any]]) -> None:
        for entry in witness:
            named.setdefault(entry["migration"], squash)
            walk(entry["migration"], entry["replaces"])

    for migration in migrations:
        walk(_dir_name(migration), migration["replaces"])
    return named


def _witnessed_name(verb: str, written: str, squash: str) -> str:
    """The refusal of a migration whose name a squash's witness records:
    every reader would take it for the original the squash replaced."""
    return (
        f"ferro migrate {verb}: {written} is the name of a migration the squash "
        f"{squash} replaces, and every reader would then take it for the replaced "
        f"original; pick another name. Nothing was written."
    )


def _refuse_data_only_over_ddl(
    parent: str | None,
    target: dict[str, Any],
    dialects: list[str],
    names: list[str],
    name: str,
    options: dict[str, Any],
) -> None:
    """Refuse ``squash --data-only`` over a range that renders DDL:
    ``--data-only`` writes the snapshot below the range, while the
    witness lets every database that ran the originals inherit it, so they
    would stand on a snapshot that says the range's DDL never ran. A range that
    renders none (what it adds it drops again) loses nothing.

    Raises:
        MigrationsDirectoryError: the plan from the parent snapshot to the
            composed target renders DDL.
    """
    try:
        raw = _generate_migration(parent, json.dumps(target), dialects)
    except ValueError as err:
        raise MigrationsDirectoryError(str(err)) from None
    if raw is None or not any(s["kind"] == "ddl" for s in json.loads(raw)["steps"]):
        return
    if len(names) == 1:
        span, render, them = names[0], "renders", "it"
    else:
        span, render, them = f"{names[0]} \u2026 {names[-1]}", "render", "them"
    raise MigrationsDirectoryError(
        f"ferro migrate squash: --data-only would drop the DDL that {span} {render}, "
        f"and every database that ran {them} would then inherit a snapshot that says "
        f"it never happened. Squash without --data-only, then add the data step with "
        f"ferro migrate new {name} --data-step {options['data_step']} --data-only. "
        f"Nothing was written."
    )


def _compose_refusal(from_: str, err: ValueError, names: list[str]) -> str:
    """A refusal of ``_compose_range``, its text kept as the core wrote it,
    then the ``--from`` a squash can pass instead, when there is one."""
    text = f"squash --from {from_}: {err}"
    at = getattr(err, "from_index", None)
    kind = getattr(err, "kind", None)
    if at is None or kind is None:
        return text
    if kind in ("uncomposable", "old_name_re_added"):
        return (
            f"{text}; pass --from {names[at]} to start the range at it, leaving the "
            f"rename below the squash"
        )
    return f"{text}; pass --from {names[at]} to start the range after {names[at - 1]}"


def _empty_squash(
    directory: Path, from_: str, names: list[str], below: str | None
) -> str:
    """The refusal of a squash that renders nothing and carries nothing."""
    range_ = names[0] if len(names) == 1 else f"{names[0]} through {names[-1]}"
    verb = "changes" if len(names) == 1 else "change"
    carry = "carries" if len(names) == 1 else "carry"
    paths = [str(directory / name) for name in names]
    if len(paths) == 1:
        delete = f"the directory {paths[0]}"
    else:
        delete = f"the directories {', '.join(paths[:-1])} and {paths[-1]}"
    return (
        f"squash --from {from_}: on top of {below or 'no migration'}, {range_} {verb} "
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
    """``migration`` as the squash of the range ``names``: each carried file
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


def _carried_steps(
    directory: Path, range_: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every data step and hand-written SQL step of the ``range_``'s migrations,
    in chain order and, within a migration, step order,
    as the generator's ``carry`` option takes them (plus each one's ``old``
    files, for the per-file lines). A dialect-suffixed SQL file is generated
    DDL: it is regenerated, never carried."""
    carry: list[dict[str, Any]] = []
    for migration in range_:
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
    the generator produced (`scaffold.render`), and each backfill that
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
    """Check, offline, that every model change has a migration and the directory is intact.

    This is `ferro migrate check`. It reads files only and takes no
    connection. It reports an ungenerated model change (naming the models),
    a broken snapshot chain (naming both files), a duplicate or missing
    number, a DDL step without the rendering a target dialect needs, and a
    data step still holding `todo(...)`. It raises nothing for a problem.

    Args:
        settings: The project's configuration. Defaults to `FerroSettings()`.
        database: The configured database. Defaults to the only one
            configured.

    Returns:
        The report; its `raise_for_problems()` raises on any problem.

    Raises:
        MigrationRefused: The configuration names no single database.

    Examples:
        In CI, fail the build on an ungenerated change:

        ```python {test=compile reason="needs a migrations project"}
        (await ferro.migrations.check()).raise_for_problems()
        ```
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
