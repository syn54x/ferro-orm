"""Where a migration's files go, and how a new one is written.

A database's migrations directory holds one directory per migration::

    migrations/
      .gitattributes                     * -text   (byte-exact: checksums are of raw bytes)
      0001_create_author/
        01_schema.up.postgres.sql        one rendering per target dialect (ADR-0026, ADR-0037)
        01_schema.down.postgres.sql
        01_schema.up.sqlite.sql
        01_schema.down.sqlite.sql
        02_fix_rows.up.sql               a hand-written step: unsuffixed, serves every dialect
        02_fix_rows.down.sql
        03_backfill_author.py            a data step: Python, up() and down() in one file
        ir.json                          the schema snapshot (ADR-0023)

The directory is read and verified by the Rust core (numbers dense from
``0001``, a duplicate refused naming both directories, the snapshot chain
intact); this module names paths, numbers the next migration and writes one
whole, so a crash never leaves a half-written migration behind.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .._core import _read_migrations_dir
from ..exceptions import FerroError

__all__ = [
    "GITATTRIBUTES",
    "SNAPSHOT_FILE",
    "SQL_STEP_PLACEHOLDER",
    "Change",
    "ChangeSummary",
    "GeneratedMigration",
    "GeneratedStep",
    "MigrationsDirectoryError",
    "Moved",
    "ensure_gitattributes",
    "next_number",
    "portable_sql_step",
    "read_migrations",
    "read_replacing",
    "write_migration",
]

SNAPSHOT_FILE = "ir.json"

GITATTRIBUTES = "* -text\n"
"""Mark every migration file byte-exact: a line-ending conversion on checkout
would change an applied step's checksum."""

SQL_STEP_PLACEHOLDER = "-- write this step\n"
"""The body of a hand-written step ``new --sql-step`` adds."""

_NAME = re.compile(r"^[a-z0-9_]+$")


class MigrationsDirectoryError(FerroError):
    """A migrations directory ``new`` or ``check`` cannot use, or a migration
    it cannot write; the message names the files involved and the fix."""


def check_name(name: str, what: str) -> None:
    """Refuse a migration or step name that is not ``[a-z0-9_]+``."""
    if not _NAME.match(name):
        raise MigrationsDirectoryError(
            f"{what} {name!r} is not usable in a file name; use lowercase "
            f"letters, digits and _ (e.g. create_author)"
        )


class Moved(StrEnum):
    """What ``ferro migrate new --replace`` or ``ferro migrate squash`` did
    with one file of the migrations it replaced."""

    KEPT = "kept"
    """A regenerated file under the name it had."""
    RENUMBERED = "renumbered"
    """A regenerated file of the same step under another number."""
    CARRIED = "carried"
    """A data step or hand-written SQL step written back byte for byte."""
    REGENERATED = "regenerated"
    """A squash's generated DDL file: planned once across the whole range,
    so it is no one original file's counterpart."""


@dataclass(frozen=True)
class GeneratedStep:
    """One step of a migration about to be written."""

    ordinal: int
    name: str
    kind: str
    """``ddl`` (one rendering per target dialect), ``portable_sql`` or
    ``data`` (a Python data step)."""
    files: dict[str, str] = field(default_factory=dict)
    """File name to its exact text."""
    carry: int | None = None
    """Carried across from the migration ``--replace`` regenerates: the
    position of the step it carries in the generator's ``carry`` option, as
    the generator names it."""
    over_namesake: bool = False
    """Carried, and in the place of a step of its name and kind the
    regeneration laid out, which it wins over."""
    role: str | None = None
    """Which kind of data step the generator laid out: ``backfill`` (a
    generated backfill), ``guard`` (``--no-backfill``) or ``data_step``
    (``--data-step``). ``None`` for every other step, and for a carried one,
    whose file is a person's whatever the generator first made it."""


def portable_sql_step(ordinal: int, name: str) -> GeneratedStep:
    """The hand-written portable step ``NN_<name>`` (``new --sql-step``) at
    ``ordinal``: its up and down files holding the placeholder."""
    stem = f"{ordinal:02d}_{name}"
    return GeneratedStep(
        ordinal,
        name,
        "portable_sql",
        {
            f"{stem}.up.sql": SQL_STEP_PLACEHOLDER,
            f"{stem}.down.sql": SQL_STEP_PLACEHOLDER,
        },
    )


@dataclass(frozen=True)
class Change:
    """One entry of a :class:`ChangeSummary`: what happened to which names
    of one kind (``new models: Team, TeamMember``), as the generator decided
    it."""

    verb: str
    """``new``, ``changed``, ``dropped``, ``renamed`` or ``removed``."""
    kind: str
    """``models``, ``enum types`` or ``enum labels``."""
    names: tuple[str, ...]
    """The names, sorted; an enum label is ``type.label``, a rename
    ``old → new``."""

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Change:
        """Build from one entry of the JSON the core returns."""
        return cls(raw["verb"], raw["kind"], tuple(raw["names"]))


@dataclass(frozen=True)
class ChangeSummary:
    """What a migration adds, changes, drops or renames (ADR-0055): the
    changes as data, in the generator's order, beside the one plain
    rendering the Rust generator produced. Lay the changes out from
    ``changes``; never take ``text`` apart."""

    changes: tuple[Change, ...] = ()
    text: str = ""
    """The plain rendering (``new models: Team; changed models: Account``),
    the one the core quotes inside ``check``'s ``ungenerated`` message and
    so the library's error text. ``new`` (in both forms) and ``check`` on a
    terminal lay out ``changes`` instead; nothing in Python prints this."""

    def __bool__(self) -> bool:
        return bool(self.changes)

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> ChangeSummary:
        """Build from the ``summary`` the core returns."""
        return cls(tuple(Change.from_json(c) for c in raw["changes"]), raw["text"])


@dataclass(frozen=True)
class GeneratedMigration:
    """A whole migration about to be written: its steps and its snapshot."""

    number: int
    name: str
    steps: tuple[GeneratedStep, ...]
    snapshot_json: str
    """The ``ir.json`` text exactly as it is to be stored."""
    summary: ChangeSummary = ChangeSummary()
    notes: tuple[str, ...] = ()
    """The lines ``new`` prints after the summary, in order: the generator's
    rename hints, then each backfill that still needs writing."""
    warnings: tuple[str, ...] = ()
    replaces: list[str] = field(default_factory=list)
    """``--replace`` and ``squash``: the replaced migrations' directory names
    (``NNNN_<name>``) in chain order, deleted once this one is written; one
    for ``--replace``, the range for a squash, empty for ``new``."""
    chained_from: str | None = None
    """``--replace`` and ``squash``: the directory name of the migration
    whose snapshot this one's links to (``None`` for the first)."""
    moves: tuple[tuple[str, str, Moved], ...] = ()
    """``--replace`` and ``squash``: ``(old file, new file, what happened)``
    for every file of this migration a replaced one had a counterpart for,
    ``old file`` relative to the migrations directory, ``new file`` to this
    migration's."""
    witness: list[dict[str, Any]] = field(default_factory=list)
    """``squash``: the witness its snapshot stores (ADR-0056), one entry per
    replaced migration in chain order, ``{"migration", "snapshot_checksum",
    "steps", "replaces"}``. Empty for ``new`` and ``--replace``."""
    dead_names: list[dict[str, Any]] = field(default_factory=list)
    """``squash``: the names born and gone inside the range, as
    ``_core._compose_range`` reports them (``{"kind", "name", "appeared",
    "vanished"}``). Empty for ``new`` and ``--replace``."""

    @property
    def squash(self) -> bool:
        """Whether this migration squashes a range (it records a witness)."""
        return bool(self.witness)

    @property
    def dir_name(self) -> str:
        return f"{self.number:04d}_{self.name}"

    @classmethod
    def from_generated(
        cls,
        raw: dict[str, Any],
        *,
        number: int,
        name: str,
        carry: Sequence[Mapping[str, Any]] = (),
    ) -> GeneratedMigration:
        """Build from the JSON ``_core._generate_migration`` returns; a
        carried step's files are the texts of the ``carry`` entry it names."""
        steps = []
        for step in raw["steps"]:
            files: dict[str, str] = {}
            stem = f"{step['ordinal']:02d}_{step['name']}"
            for dialect, rendering in step["renderings"].items():
                files[f"{stem}.up.{dialect}.sql"] = rendering["up"]
                files[f"{stem}.down.{dialect}.sql"] = rendering["down"]
            carried = step.get("carried")
            source = carry[carried["carry"]] if carried is not None else None
            if source is not None and step["kind"] == "data":
                files[f"{stem}.py"] = source["body"]
            elif source is not None:
                files[f"{stem}.up.sql"] = source["body"]
                files[f"{stem}.down.sql"] = source["down"]
            steps.append(
                GeneratedStep(
                    step["ordinal"],
                    step["name"],
                    step["kind"],
                    files,
                    carry=carried["carry"] if carried is not None else None,
                    over_namesake=bool(carried and carried["over_namesake"]),
                )
            )
        return cls(
            number=number,
            name=name,
            steps=tuple(steps),
            snapshot_json=raw["snapshot_json"],
            summary=ChangeSummary.from_json(raw["summary"]),
            notes=tuple(raw["notes"]),
            warnings=tuple(raw["warnings"]),
        )

    def files(self) -> dict[str, str]:
        """Every file of the migration, by name, ``ir.json`` last."""
        out: dict[str, str] = {}
        for step in self.steps:
            out.update(step.files)
        out[SNAPSHOT_FILE] = self.snapshot_json
        return out


def read_migrations(directory: Path) -> list[dict[str, Any]]:
    """Every migration in ``directory``, read and verified by the Rust core.

    Raises:
        MigrationsDirectoryError: the directory is malformed (a duplicate or
            missing number, a broken snapshot chain, ...), naming the fix.
    """
    try:
        raw = _read_migrations_dir(str(directory))
    except ValueError as err:
        raise MigrationsDirectoryError(f"{directory}: {err}") from None
    return json.loads(raw)["migrations"]


def read_replacing(
    directory: Path, target: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """For ``ferro migrate new --replace <target>``: every migration in
    ``directory`` but the one ``target`` names (``NNNN_<name>``, or ``NNNN``
    when one migration carries the number), read and verified by the Rust
    core with the duplicate at that number admitted, and that migration.

    Raises:
        MigrationsDirectoryError: the target is not in the directory, is a
            number two migrations share, or a later migration chains from it;
            or the rest of the directory is malformed.
    """
    try:
        raw = _read_migrations_dir(str(directory), replacing=target)
    except ValueError as err:
        raise MigrationsDirectoryError(f"{directory}: {err}") from None
    read = json.loads(raw)
    return read["migrations"], read["replaced"][0]


def read_range_reach(directory: Path, from_: str) -> dict[str, Any]:
    """For a squash: every migration in ``directory`` below the one ``from_``
    names (``NNNN_<name>``, or ``NNNN`` when one migration carries the
    number), the range from it in chain order, each read and verified by the
    Rust core, and what the range leaves off.

    Returns ``{"below", "range", "reaches_head", "off_range", "siblings"}``:
    ``range`` follows snapshot links from the migration ``from_`` names and need
    not reach the directory head. ``reaches_head`` says whether it does,
    ``off_range`` names the later migrations it leaves (a sibling chain's, or
    a fork's other branch), each ``{"migration", "chains_from"}``: its
    ``NNNN_<name>`` and the migration its snapshot chains from (``None`` for
    none in the directory), and ``siblings`` the other
    migrations at ``from_``'s number (two branches each generated it), which
    are never in ``below``; the caller refuses each with its own fix.

    Raises:
        MigrationsDirectoryError: ``from_`` is not in the directory or is a
            number two migrations share; or the directory is malformed.
    """
    try:
        raw = _read_migrations_dir(str(directory), range_from=from_)
    except ValueError as err:
        raise MigrationsDirectoryError(f"{directory}: {err}") from None
    read = json.loads(raw)
    return {
        "below": read["migrations"],
        "range": read["replaced"],
        "reaches_head": read["reaches_head"],
        "off_range": read["off_range"],
        "siblings": read["siblings"],
    }


def next_number(directory: Path) -> int:
    """The number the next migration in ``directory`` takes (``1`` when empty)."""
    return len(read_migrations(directory)) + 1


def ensure_gitattributes(directory: Path) -> None:
    """Write ``directory/.gitattributes`` (``* -text``) when it is absent."""
    path = directory / ".gitattributes"
    if not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        path.write_bytes(GITATTRIBUTES.encode())


def write_migration(directory: Path, migration: GeneratedMigration) -> Path:
    """Write ``migration`` under ``directory`` and return its directory.

    The files are written into a hidden sibling first and moved into place
    with one rename, so the directory reader never sees half a migration.
    Every file is written as raw UTF-8 bytes, never newline-translated.

    Raises:
        MigrationsDirectoryError: the migration's directory, or its hidden
            staging sibling (another run, or an interrupted one), already exists.
    """
    target = directory / migration.dir_name
    if target.exists():
        raise MigrationsDirectoryError(
            f"{target} already exists; a migration directory is never overwritten"
        )
    directory.mkdir(parents=True, exist_ok=True)
    staging = directory / f".{migration.dir_name}.partial"
    if staging.exists():
        raise MigrationsDirectoryError(
            f"{staging} exists: another `ferro migrate new` is writing "
            f"{migration.dir_name}, or one was interrupted. Nothing was written; "
            "`ferro migrate check` names how to recover it"
        )
    staging.mkdir()
    try:
        for name, text in migration.files().items():
            (staging / name).write_bytes(text.encode("utf-8"))
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target
