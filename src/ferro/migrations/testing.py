"""The migration test harness: a project's own tests prove its chain (ADR-0045).

Migration ``0012_add_slug`` adds ``author.slug`` and backfills it in a data
step. The test seeds a row the way the table stood at ``0011``, applies
exactly ``0012``, and reads the row back the way ``0012`` left it::

    from ferro.migrations.testing import harness

    async def test_slug_backfill(db_url, migrations_settings):
        await ferro.connect(db_url)
        h = harness(settings=migrations_settings, database="default")
        await h.apply_through("0011")
        async with h.models_at("0011") as models:
            await models.Author(name="Ada").save()   # seed against the schema that existed
        await h.apply("0012")                        # refused unless the database stands at 0011
        async with h.models_at("0012") as models:
            ada = await models.Author.where(lambda author: author.name == "Ada").first()
            assert ada.slug == "ada"
        await h.revert_to("0011")
        await h.round_trip()

and the test that every down reaches its parent is one line::

    async def test_the_chain_round_trips(db_url):
        await ferro.connect(db_url)
        result = await harness().round_trip()
        assert result.irreversible is None

The harness carries what the application's own calls refuse (ADR-0040,
ADR-0038): a target on the way up, a revert with no prompt, and the
historical models outside a data step. It is one object per fixture,
binding ``settings=``, ``database=`` and ``using=`` once and keeping no
other state: every call reads where the database stands from its tracking
table. It never makes a database fresh and never skips a dialect (both are
the project's fixture), and it has no ``reset()`` and no step-level target
(``"0007:02"``): no snapshot describes the state between two steps.

Nothing here decides how a migration runs. Every step runs through the
runner (:func:`ferro.migrations.runner.up` / :func:`~ferro.migrations.runner.down`)
under its run lock, every comparison is :func:`ferro.migrations.drift`, and
every historical model is built by
:func:`ferro.migrations.historical.build_single` and installed with
:meth:`ferro.registry.Registry.swap`. A harness call that cannot do exactly
what it was asked raises :class:`~ferro.migrations.errors.MigrationRefused`
naming the migration the database stands at and the one asked for; the
runner's own refusals surface unchanged. Only ``apply*``, ``revert*`` and
``round_trip`` take the run lock or create the tracking tables.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import _core
from ..registry import REGISTRY
from ..session import engines
from . import historical, runner
from .api import _connection, _resolve
from .drift import drift, render_op
from .errors import MigrationRefused

if TYPE_CHECKING:
    from ..settings import DatabaseSettings, FerroSettings
    from .context import HistoricalModels
    from .runner import RunReport

__all__ = ["Harness", "RoundTripResult", "harness"]

_MIGRATION = re.compile(r"(\d{4})(_\w+)?")
# The one spelling both producers of the refusal use: the run planner
# (`RunRefusal::Irreversible`) and the runner's data-step loader.
_IRREVERSIBLE = re.compile(r"ferro migrate: (\d{4}):(\d{2}) is irreversible: (.+)")
_APPLIED = {"installed", "installed (baseline)", "installed (different checksum)"}


@dataclass(frozen=True)
class RoundTripResult:
    """What :meth:`Harness.round_trip` walked.

    A chain whose every down reaches its parent::

        RoundTripResult(applied=["0001_create_author", "0002_add_slug"],
                        reverted_to=None, irreversible=None)

    and one whose ``0002`` declares a step irreversible::

        RoundTripResult(applied=["0001_create_author", "0002_add_slug"],
                        reverted_to="0002_add_slug",
                        irreversible=("0002_add_slug", "01_schema", "the index is shared"))
    """

    applied: list[str] = field(default_factory=list)
    """Every migration of the chain, in order (``NNNN_<name>``): each stood
    applied with no drift on the way up, and again on the way back up."""
    reverted_to: str | None = None
    """Where the downward walk stopped: the migration holding the
    irreversible step, still applied. ``None`` when it reverted everything."""
    irreversible: tuple[str, str, str] | None = None
    """``(migration, step, reason)`` of the irreversible step that ended the
    downward walk, or ``None``."""


@dataclass(frozen=True)
class _Migration:
    number: int
    name: str
    """``NNNN_<name>``."""
    dir: Path
    snapshot: dict[str, Any]
    steps: dict[int, str]
    """Step ordinal → ``NN_<name>``."""

    @property
    def short(self) -> str:
        return f"{self.number:04}"


class Harness:
    """A migration chain driven by name, for a test. Build it with
    :func:`harness`."""

    def __init__(
        self, settings: FerroSettings, database: DatabaseSettings, using: str | None
    ) -> None:
        self._settings = settings
        self._database = database
        self._using = using

    def __repr__(self) -> str:
        on = self._using if self._using is not None else "the default connection"
        return f"<Harness {self._database.name} on {on}>"

    # -- the verbs ----------------------------------------------------------------

    async def apply_through(self, migration: str) -> RunReport:
        """Apply every pending migration up to and including ``migration``
        (``"0007"`` or ``"0007_split_name"``), under the run lock.

        A database already standing at ``migration`` is left alone (an empty
        report). Refused naming both when the database stands past it.
        """
        chain = self._chain()
        target = _find(chain, migration)
        name = _connection(self._using)
        at = await self._standing(name, f"apply_through({migration!r})")
        if at > target:
            raise MigrationRefused(
                f"harness.apply_through({migration!r}): the database stands at "
                f"{chain[at].name}, past {chain[target].name}; nothing was applied. "
                f"Revert to it first with revert_to({chain[target].short!r})."
            )
        if at == target:
            return runner.RunReport()
        return await self._up_through(name, chain, target)

    async def apply(self, migration: str) -> RunReport:
        """Apply exactly ``migration``, under the run lock.

        Refused, naming the migration the database stands at and the parent
        it would have to stand at, unless it stands at that parent (for the
        first migration: at nothing), so a test that seeded at ``0006``
        knows only ``0007`` ran.
        """
        chain = self._chain()
        target = _find(chain, migration)
        name = _connection(self._using)
        at = await self._standing(name, f"apply({migration!r})")
        if at != target - 1:
            parent = (
                f"{chain[target - 1].name}, the parent of {chain[target].name}"
                if target > 0
                else f"no migration, below {chain[target].name}"
            )
            hint = (
                f" Use apply_through({chain[target].short!r}) to apply every "
                f"migration up to it."
                if at < target - 1
                else ""
            )
            raise MigrationRefused(
                f"harness.apply({migration!r}): the database stands at "
                f"{_stands(chain, at)}, not at {parent}; nothing was applied.{hint}"
            )
        return await self._up_through(name, chain, target)

    async def revert_to(self, migration: str) -> RunReport:
        """Revert every migration above ``migration``, newest first, with no
        prompt, leaving ``migration`` applied.

        The runner's rules hold: an irreversible step in the way refuses
        before anything is reverted. Refused naming both when the database
        stands below ``migration``.
        """
        chain = self._chain()
        target = _find(chain, migration)
        name = _connection(self._using)
        at = await self._standing(name, None)
        if at < target:
            raise MigrationRefused(
                f"harness.revert_to({migration!r}): the database stands at "
                f"{_stands(chain, at)}, below {chain[target].name}; nothing was "
                f"reverted. Apply it with apply_through({chain[target].short!r})."
            )
        return await self._down(name, target=chain[target].short)

    async def revert_all(self) -> RunReport:
        """Revert every applied migration, newest first, with no prompt. On a
        database with nothing applied it does nothing and creates nothing."""
        return await self._down(_connection(self._using), all=True)

    @asynccontextmanager
    async def models_at(self, migration: str) -> AsyncIterator[HistoricalModels]:
        """The historical models of ``migration``'s snapshot alone, installed
        for the block: the same ``models`` object a data step's context
        carries, with every table as that one snapshot declares it (a column
        a later migration adds is absent, not nullable)::

            async with h.models_at("0011") as models:
                await models.Author(name="Ada").save()
                links = await models.table("author_tags").all()

        The block is a session on the harness's connection, so the models
        need no ``using=``. While it is open today's classes are unreachable
        (``Author`` raises naming ``models.Author``); they are restored on
        exit, error and cancellation. Reads no database state.
        """
        chain = self._chain()
        this = chain[_find(chain, migration)]
        name = _connection(self._using)
        models = historical.build_single(this.snapshot, this.name)
        with REGISTRY.swap(models):
            async with engines.session(name):
                yield models

    async def round_trip(self) -> RoundTripResult:
        """Apply every migration, revert every one, and apply them all again,
        one migration per stop, raising :class:`MigrationRefused` with the
        drift lines at the first stop where the database is not what that
        migration's snapshot declares.

        A step whose down is declared irreversible ends the downward walk at
        its migration: the result reports it (``irreversible``,
        ``reverted_to``) and the walk back up completes; it is not a failure.
        """
        chain = self._chain()
        name = _connection(self._using)
        at = await self._standing(name, "round_trip()")
        if at >= 0:
            await self._assert_clean(name, chain[at], "the database as found")
        at = await self._walk_up(name, chain, at)

        reverted_to: str | None = None
        irreversible: tuple[str, str, str] | None = None
        while at >= 0:
            below = chain[at - 1].short if at > 0 else "0000"
            report = await runner.down(
                self._settings, self._database, target=below, using=name
            )
            if report.refusal is not None:
                irreversible = _irreversible(report.refusal, chain)
                if irreversible is None:
                    raise MigrationRefused(report.refusal)
                reverted_to = chain[at].name
                break
            stop = f"reverting {chain[at].name}"
            at -= 1
            if at >= 0:
                await self._assert_clean(name, chain[at], stop)
            else:
                await self._assert_empty(name, chain[0], stop)

        await self._walk_up(name, chain, at)
        return RoundTripResult(
            applied=[m.name for m in chain],
            reverted_to=reverted_to,
            irreversible=irreversible,
        )

    # -- composition --------------------------------------------------------------

    def _chain(self) -> list[_Migration]:
        """The migrations directory, read and verified."""
        try:
            raw = json.loads(_core._read_migrations_dir(str(self._database.directory)))
        except ValueError as err:
            raise MigrationRefused(str(err)) from None
        return [
            _Migration(
                number=m["number"],
                name=Path(m["dir"]).name,
                dir=Path(m["dir"]),
                snapshot=m["snapshot"]["ir"],
                steps={
                    s["ordinal"]: f"{s['ordinal']:02}_{s['name']}" for s in m["steps"]
                },
            )
            for m in raw["migrations"]
        ]

    async def _standing(self, name: str, verb: str | None) -> int:
        """The index of the last migration fully applied (``-1``: none),
        read without the lock. ``verb`` going up refuses a migration left
        part-way; going down (``None``) the runner resumes it."""
        status = await runner.status(self._settings, self._database, using=name)
        if status.ahead:
            raise MigrationRefused(
                f"harness: the database has applied {', '.join(status.ahead)}, which "
                f"{self._database.directory} does not hold; nothing was changed."
            )
        at = -1
        for index, migration in enumerate(status.migrations):
            state = migration.state
            if state in _APPLIED and at == index - 1:
                at = index
            elif state not in _APPLIED and state != "pending" and verb is not None:
                raise MigrationRefused(
                    f"harness.{verb}: {migration.name} is {state}; nothing was "
                    f"applied. Revert it with revert_to(...) or revert_all() first."
                )
        if status.refusal is not None and verb is not None:
            raise MigrationRefused(status.refusal)
        return at

    async def _up_through(
        self, name: str, chain: list[_Migration], target: int
    ) -> RunReport:
        """``up`` over the chain as it stood when ``chain[target]`` was its
        head: a copy of the directory holding only the migrations through it,
        so the runner plans and runs exactly their pending steps, byte for
        byte the files on disk, under its own lock and records."""
        with tempfile.TemporaryDirectory(prefix="ferro-harness-") as tmp:
            through = Path(tmp) / self._database.directory.name
            through.mkdir()
            for migration in chain[: target + 1]:
                shutil.copytree(migration.dir, through / migration.name)
            database = self._database.model_copy(update={"directory": through})
            report = await runner.up(self._settings, database, using=name)
        if report.refusal is not None:
            raise MigrationRefused(report.refusal)
        return report

    async def _down(
        self, name: str, *, target: str | None = None, all: bool = False
    ) -> RunReport:
        report = await runner.down(
            self._settings, self._database, target=target, all=all, using=name
        )
        if report.refusal is not None:
            raise MigrationRefused(report.refusal)
        return report

    async def _walk_up(self, name: str, chain: list[_Migration], at: int) -> int:
        """Apply one migration per stop from ``at`` to the head, checking
        drift after each."""
        for target in range(at + 1, len(chain)):
            await self._up_through(name, chain, target)
            await self._assert_clean(
                name, chain[target], f"applying {chain[target].name}"
            )
        return len(chain) - 1

    async def _assert_clean(self, name: str, at: _Migration, stop: str) -> None:
        report = await drift(self._settings, self._database.name, using=name)
        if not report.clean:
            detail = report.refusal if report.refusal is not None else report.render()
            raise MigrationRefused(
                f"harness.round_trip(): after {stop}, the database is not what "
                f"{at.name} declares:\n{detail}"
            )

    async def _assert_empty(self, name: str, first: _Migration, stop: str) -> None:
        """With every migration reverted there is no snapshot to compare
        with: none of the first migration's tables may remain."""
        live = set(json.loads(await _core._live_tables(name)))
        left = [
            model["table_name"]
            for model in first.snapshot["payload"]["models"]
            if model["table_name"] in live
        ]
        if left:
            lines = "\n".join(
                f"  {render_op({'kind': 'DropTable', 'table': t})}" for t in left
            )
            raise MigrationRefused(
                f"harness.round_trip(): after {stop}, the database should hold "
                f"none of its tables:\n{lines}"
            )


def _find(chain: list[_Migration], migration: str) -> int:
    """The index of ``migration`` (``"0007"`` or ``"0007_split_name"``)."""
    match = _MIGRATION.fullmatch(migration.strip())
    if match is not None:
        number = int(match.group(1))
        for index, this in enumerate(chain):
            if this.number == number and (
                match.group(2) is None or this.name == migration
            ):
                return index
    holds = ", ".join(m.name for m in chain) or "no migrations"
    raise MigrationRefused(
        f"harness: there is no migration {migration!r}; the chain holds {holds}. "
        f"Name one by its number (0007) or its directory (0007_split_name); a step "
        f"(0007:02) is not a target, since no snapshot describes the state between "
        f"two steps."
    )


def _stands(chain: list[_Migration], at: int) -> str:
    return chain[at].name if at >= 0 else "no migration (nothing is applied)"


def _irreversible(refusal: str, chain: list[_Migration]) -> tuple[str, str, str] | None:
    """``(migration, step, reason)`` when ``refusal`` is the runner's
    irreversible-step refusal, else ``None``."""
    match = _IRREVERSIBLE.match(refusal.splitlines()[0])
    if match is None:
        return None
    number, ordinal, reason = int(match.group(1)), int(match.group(2)), match.group(3)
    migration = next(m for m in chain if m.number == number)
    return migration.name, migration.steps[ordinal], reason


def harness(
    *,
    settings: FerroSettings | None = None,
    database: str | None = None,
    using: str | None = None,
) -> Harness:
    """A :class:`Harness` over ``database`` (``None``: the one configured) of
    ``settings`` (``None``: ``FerroSettings()``), working on the open
    connection ``using`` (``None``: the default connection), which the test
    connects and makes fresh itself::

        @pytest.fixture
        async def h(db_url):
            await ferro.connect(db_url)
            return harness()

    Raises:
        MigrationRefused: no configuration is found, or ``database`` names
            none of its databases.
    """
    settings, db = _resolve(settings, database)
    return Harness(settings, db, using)
