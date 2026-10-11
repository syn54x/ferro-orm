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
    """What ``ferro migrate new --replace`` did with one file of the
    migration it replaced."""

    KEPT = "kept"
    """A regenerated file under the name it had."""
    RENUMBERED = "renumbered"
    """A regenerated file of the same step under another number."""
    CARRIED = "carried"
    """A data step or hand-written SQL step written back byte for byte."""


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
    carried: bool = False
    """Carried across from the migration ``--replace`` regenerates."""
    over_namesake: bool = False
    """Carried, and in the place of a step of its name and kind the
    regeneration laid out, which it wins over."""


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
class GeneratedMigration:
    """A whole migration about to be written: its steps and its snapshot."""

    number: int
    name: str
    steps: tuple[GeneratedStep, ...]
    snapshot_json: str
    """The ``ir.json`` text exactly as it is to be stored."""
    summary: str = ""
    warnings: tuple[str, ...] = ()
    replaces: str | None = None
    """``--replace``: the replaced migration's directory name (``NNNN_<name>``),
    deleted once this one is written."""
    chained_from: str | None = None
    """``--replace``: the directory name of the migration whose snapshot this
    one's links to (``None`` for the first)."""
    moves: tuple[tuple[str, str, Moved], ...] = ()
    """``--replace``: ``(old file, new file, what happened)`` for every file
    of this migration the replaced one had a counterpart for, ``old file``
    relative to the migrations directory, ``new file`` to this migration's."""

    @property
    def dir_name(self) -> str:
        return f"{self.number:04d}_{self.name}"

    @classmethod
    def from_generated(
        cls, raw: dict[str, Any], *, number: int, name: str
    ) -> GeneratedMigration:
        """Build from the JSON ``_core._generate_migration`` returns."""
        steps = []
        for step in raw["steps"]:
            files: dict[str, str] = {}
            stem = f"{step['ordinal']:02d}_{step['name']}"
            for dialect, rendering in step["renderings"].items():
                files[f"{stem}.up.{dialect}.sql"] = rendering["up"]
                files[f"{stem}.down.{dialect}.sql"] = rendering["down"]
            carried = step.get("carried")
            if carried is not None and step["kind"] == "data":
                files[f"{stem}.py"] = carried["body"]
            elif carried is not None:
                files[f"{stem}.up.sql"] = carried["body"]
                files[f"{stem}.down.sql"] = carried["down"]
            steps.append(
                GeneratedStep(
                    step["ordinal"],
                    step["name"],
                    step["kind"],
                    files,
                    carried=carried is not None,
                    over_namesake=bool(carried and carried["over_namesake"]),
                )
            )
        return cls(
            number=number,
            name=name,
            steps=tuple(steps),
            snapshot_json=raw["snapshot_json"],
            summary=raw["summary"],
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
    return read["migrations"], read["replaced"]


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
        MigrationsDirectoryError: the migration's directory already exists.
    """
    target = directory / migration.dir_name
    if target.exists():
        raise MigrationsDirectoryError(
            f"{target} already exists; a migration directory is never overwritten"
        )
    directory.mkdir(parents=True, exist_ok=True)
    staging = directory / f".{migration.dir_name}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        for name, text in migration.files().items():
            (staging / name).write_bytes(text.encode("utf-8"))
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target
