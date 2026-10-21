"""What `ferro migrate status` reports, and how it prints.

```text
$ ferro migrate status
default (postgres) · public._ferro_migrations

0006_add_teams           applied
0007_nickname            partial, 1 of 3 steps
  01_add_nickname.sql      applied
  02_backfill_nickname.py  failed at 30,000 rows
    ValueError: nickname too long (id=30000412)
  03_nickname_index.sql    pending
0008_drop_legacy         pending
```

A fully applied or fully pending migration is one line; its steps expand
where something needs attention, or everywhere with `--steps`. Every state
is decided in the core; this module names and prints them (a chunked
step's `rows_done` comes from its record), and is
the one place that reads them into where the database stands: its last
fully applied migration, the one a run left unfinished, and whether
anything is pending. `status` exits 3 when anything is pending and 4 when
anything needs attention (a failed, interrupted or reverting step, an edited
file, a database ahead of the checkout, or a refusal `up` would meet).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import MigrationRefused

__all__ = [
    "AlreadyTrackedError",
    "AppliedAboveBaselineError",
    "MigrationStatus",
    "RunRefused",
    "StatusReport",
    "StepStatus",
]


class RunRefused(MigrationRefused):
    """A migration run's refusal before it ran anything, naming the fix.

    Raised for every refusal the run planner, the run lock and the tracking
    tables make, and by `connect(auto_migrate=True)` on a database that
    migrations manage; its message is the operator's text. It is a
    `MigrationRefused`, so `except MigrationRefused` catches it too. Some
    refusals also carry what they are about in `kind`, so a caller matches
    on it rather than on the text; on the others `kind` is `None`. A kind with a class of its own
    (`AlreadyTrackedError`, `AppliedAboveBaselineError`) arrives as that
    class.

    Examples:
        ```python {test=compile reason="needs a project with a migrations directory"}
        try:
            await ferro.migrations.up()
        except ferro.migrations.RunRefused as refused:
            if refused.kind == "edited_applied":
                print(refused)   # names the applied migration that was edited
            raise
        ```
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str | None = None,
        migration: int | None = None,
        step: int | None = None,
        reason: str | None = None,
        ahead_only: bool = False,
        contract_steps: Sequence[str] | None = None,
        names: Sequence[str] | None = None,
        head: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        """The refusal's kind (`"irreversible"`, `"edited_applied"`, ...), or `None`.

        Only some refusals carry a kind. `connect(auto_migrate=True)` on a
        database that migrations manage, a lost run lock and a data step
        that does not load are among those whose `kind` is `None`; match
        those on the message, or catch the `MigrationRefused` base.
        """
        self.migration = migration
        """The migration number the refusal names, or `None`."""
        self.step = step
        """The step number the refusal names, or `None`."""
        self.reason = reason
        """An irreversible step's declared reason, or `None`."""
        self.ahead_only = ahead_only
        """Whether `allow_ahead` alone would have let the run through.

        True when the database holds migrations the directory lacks and
        nothing else stood in the way.
        """
        self.contract_steps = list(contract_steps or ())
        """With `ahead_only`, each step of those migrations whose class is not `expand`.

        Each is `NNNN_<name>:NN`: what `allow_ahead="expand-only"` refuses
        on. Empty otherwise.
        """
        self.names = list(names or ())
        """The migrations (`NNNN_<name>`) a refusal about a set of them names.

        A tracked database's records, or the run-applied migrations above a
        baseline. Empty otherwise.
        """
        self.head = head
        """The newest of a tracked database's records, for an already-tracked refusal; `None` otherwise."""

    @classmethod
    def from_core(
        cls, message: str, *, kind: str | None = None, **payload: Any
    ) -> RunRefused:
        """Build the refusal for `kind`, as the subclass a caller branches on when there is one.

        The class is chosen by `kind` alone, never by `message`.

        Args:
            message: The refusal's text, naming the fix.
            kind: The planner's refusal kind, or `None` for a refusal that is
                not the planner's.
            **payload: The other keyword data `RunRefused` takes.

        Returns:
            An `AlreadyTrackedError` or `AppliedAboveBaselineError` when the
                kind has one, a plain `RunRefused` otherwise.
        """
        return _BY_KIND.get(kind or "", RunRefused)(message, kind=kind, **payload)


class AlreadyTrackedError(RunRefused):
    """The refusal `baseline()` raises for a database that is already tracked.

    A baseline records only on a database whose tracking table holds no
    step records; nothing was recorded. Two pre-deploys that adopt one
    database at once both find no records and both call `baseline()`; the
    run lock lets one record, and the other raises this, so the caller
    carries on to `up()`. The target is the migration the previous
    release's models generated, so an unattended baseline records no step
    it did not run. Its `head` is the newest of the records, and its
    `report` the database's `StatusReport`, what `status()` reports.

    Examples:
        ```python {test=compile reason="needs a migrations project and a database"}
        try:
            await ferro.migrations.baseline(target="0001")
        except ferro.migrations.AlreadyTrackedError:
            pass
        await ferro.migrations.up()
        ```
    """

    def __init__(self, message: str, **payload: Any) -> None:
        super().__init__(message, **payload)
        self.applied = list(self.names)
        """Every migration the records name, as `NNNN_<name>`, in order."""


class AppliedAboveBaselineError(RunRefused):
    """The refusal `remove_baseline()` raises when a run applied migrations above the baseline.

    Removing the baseline under them would leave applied migrations above
    pending ones. The message names the `ferro migrate down --to` that
    reverts them; nothing was removed. Its `report` is the database's
    `StatusReport`.
    """

    def __init__(self, message: str, **payload: Any) -> None:
        super().__init__(message, **payload)
        self.above = list(self.names)
        """The run-applied migrations above the baseline, as `NNNN_<name>`."""


_BY_KIND: dict[str, type[RunRefused]] = {
    "already_tracked": AlreadyTrackedError,
    "applied_above_baseline": AppliedAboveBaselineError,
}
"""The run planner refusal kinds a caller branches on by type."""


@dataclass(frozen=True)
class _State:
    """What one step state means to every reader of a status report."""

    word: str
    """The word the core serializes (``applied_baseline``)."""
    shown: str
    """The word ``status`` prints (``applied (baseline)``)."""
    applied: bool = False
    """The step has a finished step record, so it is not pending."""
    unfinished: bool = False
    """A run left the step part-way, or is in it."""
    running: bool = False
    """A run holding the run lock is in the step."""
    attention: bool = False
    """A person has to look before anything runs (exit 4)."""
    whole: bool = False
    """A migration whose every step is in this state prints on one line."""


_STATES = (
    _State("applied", "applied", applied=True, whole=True),
    _State(
        "applied_different_checksum",
        "applied (different checksum)",
        applied=True,
        attention=True,
    ),
    _State("applied_baseline", "applied (baseline)", applied=True, whole=True),
    _State("applied_inherited", "applied (inherited)", applied=True, whole=True),
    _State("pending", "pending", whole=True),
    _State("pending_inheritable", "pending (inheritable)", whole=True),
    _State("running", "running", unfinished=True, running=True),
    _State("failed", "failed", unfinished=True, attention=True),
    _State("interrupted", "interrupted", unfinished=True, attention=True),
    _State("reverting", "reverting", unfinished=True, attention=True),
)
"""The one Python reading of the core's step states (``StepState`` in
``crates/ferro-migrate/src/run_plan.rs``). Every question this module
answers about where a step, a migration or a database stands is read from
this table; ``drift`` and the test harness ask the report rather than keep
their own copy."""


def _state(*, word: str | None = None, shown: str | None = None) -> _State:
    """The row of `_STATES` with this core ``word`` or this ``shown`` word."""
    for state in _STATES:
        if state.word == word or state.shown == shown:
            return state
    raise ValueError(f"no step state {word or shown!r}; the core and report disagree")


_RUNNING = next(state for state in _STATES if state.running)


def migration_span(names: Sequence[str]) -> str:
    """Name a run of migrations as `<first> … <last>`, or the one name.

    This is how an inheritance names what the database applied.

    Args:
        names: Migration names, in chain order.

    Returns:
        The first and last names joined by an ellipsis, or the only name.
    """
    return names[0] if len(names) == 1 else f"{names[0]} … {names[-1]}"


@dataclass(frozen=True)
class StepStatus:
    """One step of one migration."""

    step: int
    """The step's number within its migration."""
    file: str
    """The file this database executes (or executed)."""
    state: str
    """The state `status` prints, such as `applied`, `pending (inheritable)` or `failed`.

    One of `applied`, `applied (different checksum)`, `applied (baseline)`,
    `applied (inherited)`, `pending`, `pending (inheritable)` (a squash the
    next run inherits), `running`, `failed`, `interrupted` or `reverting`.
    """
    error: str | None = None
    """The last attempt's error, when recorded."""
    applied_checksum: str | None = None
    """The file's checksum when it was applied, or `None` for a step with no record."""
    on_disk_checksum: str | None = None
    """The file's checksum as it is on disk now."""
    flags: list[str] = field(default_factory=list)
    """The `destructive` / `data-dependent` headers the file carries."""
    step_class: str = "contract"
    """`expand` or `contract`: whether the previous release's writes keep succeeding beside the step.

    The class its record holds, read as `contract` when a record an older
    Ferro wrote holds none; for a step with no record, the class a run would
    record.
    """
    rows_done: int | None = None
    """The rows a chunked step's current walk has committed."""
    finished_at: str | None = None
    """When the step's record finished, as stored (ISO-8601 UTC, `Z`).

    A baseline record's is the baseline time. `None` for a step with no
    finished record (pending, running, failed, interrupted).
    """

    @property
    def shown(self) -> str:
        """The state as `status` prints it, with how far a failed chunked step got (`failed at 30,000 rows`)."""
        if self.state == "failed" and self.rows_done is not None:
            return f"failed at {self.rows_done:,} rows"
        return self.state

    @property
    def applied(self) -> bool:
        """Whether the step has a finished step record (any of the `applied` states)."""
        return _state(shown=self.state).applied

    @property
    def unfinished(self) -> bool:
        """Whether a run left this step part-way, or is in it (`running`, `failed`, `interrupted` or `reverting`)."""
        return _state(shown=self.state).unfinished

    @property
    def running(self) -> bool:
        """Whether a run holding the run lock is in this step."""
        return _state(shown=self.state).running

    @property
    def needs_attention(self) -> bool:
        """Whether a person has to look before anything runs (exit 4)."""
        return _state(shown=self.state).attention

    @property
    def pending(self) -> bool:
        """Whether the step is still to apply (it has no finished step record)."""
        return not self.applied


@dataclass(frozen=True)
class MigrationStatus:
    """One migration and its steps."""

    number: int
    """The migration's number."""
    name: str
    """The migration's directory name, `NNNN_<name>`."""
    steps: list[StepStatus]
    """Its steps, in order."""
    inherits: list[str] = field(default_factory=list)
    """For a squash pending its inheritance, each migration the database applied in its place.

    Each is `NNNN_<name>`, in chain order. Empty otherwise.
    """

    @property
    def hint(self) -> str | None:
        """What `status` says to do about this migration on its row, or `None`.

        For a squash pending inheritance: ``run `ferro migrate up`; this
        database applied <first> … <last> and nothing will be run``.
        """
        if not self.inherits:
            return None
        return (
            f"run `ferro migrate up`; this database applied "
            f"{migration_span(self.inherits)} and nothing will be run"
        )

    @property
    def applied_at(self) -> str | None:
        """When the migration was applied: the last finished step's `finished_at`, or `None`.

        A partial migration reports when it stopped.
        """
        return next(
            (s.finished_at for s in reversed(self.steps) if s.finished_at), None
        )

    @property
    def applied(self) -> bool:
        """Whether every step is applied."""
        return bool(self.steps) and all(step.applied for step in self.steps)

    @property
    def pending(self) -> bool:
        """Whether a step is still to apply."""
        return any(step.pending for step in self.steps)

    @property
    def unfinished_step(self) -> StepStatus | None:
        """The first step a run left part-way (or is in), if any."""
        return next((step for step in self.steps if step.unfinished), None)

    @property
    def unfinished(self) -> bool:
        """Whether a run left this migration part-way.

        True when a step is unfinished, or only some of its steps are
        applied. A wholly pending migration is not.
        """
        return self.unfinished_step is not None or (
            any(step.applied for step in self.steps) and not self.applied
        )

    @property
    def running(self) -> bool:
        """Whether a run holding the run lock is in this migration."""
        return any(step.running for step in self.steps)

    @property
    def _whole(self) -> str | None:
        """The one state every step is in, when it prints on one line."""
        words = {step.state for step in self.steps}
        if len(words) == 1:
            word = next(iter(words))
            if _state(shown=word).whole:
                return word
        return None

    @property
    def state(self) -> str:
        """The migration's state as `status` prints it on its row.

        The steps' one state when every step agrees (`applied`, `applied
        (baseline)`, `applied (inherited)`, `pending` or `pending
        (inheritable)`); `running` while a run is in it; `applied (different
        checksum)` when every step is applied and one changed; else
        `partial, X of N steps`.
        """
        whole = self._whole
        if whole is not None:
            return whole
        if self.running:
            return _RUNNING.shown
        if not self.pending:
            return "applied (different checksum)"
        done = sum(1 for step in self.steps if step.applied)
        return f"partial, {done} of {len(self.steps)} steps"

    @property
    def expands(self) -> bool:
        """Whether `status` prints this migration's steps without `--steps`."""
        return self._whole is None


@dataclass(frozen=True)
class StatusReport:
    """Where a database stands against its migrations: what `status()` returns.

    `ferro migrate status` prints it. It also says where the database
    stands for every caller that has to know (`drift`, the test harness).
    For a database with `0006_add_teams` applied and `0007_nickname`
    part-way:

    ```python {test=compile reason="needs a database with a migration part-way applied"}
    assert report.head_applied.name == "0006_add_teams"
    assert report.unfinished.name == "0007_nickname"
    assert report.pending
    ```
    """

    database: str
    """The configured database's name."""
    url: str
    """The database's URL, its secrets masked by `***`."""
    dialect: str
    """The database's dialect, `postgres` or `sqlite`."""
    table: str
    """The tracking table, `<schema>._ferro_migrations`."""
    migrations: list[MigrationStatus]
    """Every migration in the directory, in order."""
    ahead: list[str] = field(default_factory=list)
    """Migrations this database has applied that the directory lacks.

    Never the originals of a squash the database is pending the inheritance
    of.
    """
    inheritable: list[str] = field(default_factory=list)
    """Each squash the next run inherits (`NNNN_<name>`), running nothing, whatever `allow_ahead` says."""
    refusal: str | None = None
    """What `up` would refuse with, when it would."""
    refusal_needs_attention: bool = False
    """Whether `refusal` is one a person has to look at (exit 4) rather than a pending state."""

    @property
    def head_applied(self) -> MigrationStatus | None:
        """The last fully applied migration, or `None` when no migration is.

        Its schema snapshot is the one the database is measured against.
        """
        applied = [m for m in self.migrations if m.applied]
        return applied[-1] if applied else None

    @property
    def unfinished(self) -> MigrationStatus | None:
        """The first migration a run left part-way (or is in), or `None`.

        The database stands at neither its parent's snapshot nor its own.
        """
        return next((m for m in self.migrations if m.unfinished), None)

    @property
    def pending(self) -> bool:
        """Whether anything is still to apply."""
        return any(m.pending for m in self.migrations)

    @property
    def needs_attention(self) -> bool:
        """Whether a person has to look before anything runs."""
        return (
            bool(self.ahead)
            or (self.refusal is not None and self.refusal_needs_attention)
            or any(step.needs_attention for m in self.migrations for step in m.steps)
        )

    @property
    def exit_code(self) -> int:
        """The exit code `status` exits with: 4 when anything needs attention, 3 when anything is pending, else 0."""
        # Imported here so the in-process API never loads the CLI package.
        from ..cli import exit_codes

        if self.needs_attention:
            return exit_codes.NEEDS_ATTENTION
        if self.pending:
            return exit_codes.PENDING
        return exit_codes.OK

    @classmethod
    def from_core(
        cls,
        raw: dict[str, Any],
        *,
        database: str,
        url: str,
        dialect: str,
        table: str,
        refusal: str | None = None,
        rows_done: dict[tuple[int, int], int] | None = None,
        finished_at: dict[tuple[int, int], str | None] | None = None,
    ) -> StatusReport:
        """Build the report from the tracked database's status document.

        Args:
            raw: The status document the core returns.
            database: The configured database's name.
            url: The database's URL, its secrets masked.
            dialect: The database's dialect.
            table: The tracking table, `<schema>._ferro_migrations`.
            refusal: A refusal that replaces the document's own, when given.
            rows_done: Each chunked step's committed rows, by
                `(migration, step)`.
            finished_at: Each step record's stored finish time, by
                `(migration, step)`.

        Returns:
            The report.
        """
        rows_done = rows_done or {}
        finished_at = finished_at or {}
        migrations = [
            MigrationStatus(
                number=m["number"],
                name=m["name"],
                steps=[
                    StepStatus(
                        step=s["step"],
                        file=s["file"],
                        state=_state(word=s["state"]).shown,
                        error=s["error"],
                        applied_checksum=s["applied_checksum"],
                        on_disk_checksum=s["on_disk_checksum"],
                        flags=list(s["flags"]),
                        step_class=s["class"],
                        rows_done=rows_done.get((m["number"], s["step"])),
                        finished_at=finished_at.get((m["number"], s["step"])),
                    )
                    for s in m["steps"]
                ],
                inherits=list(m.get("inherits", ())),
            )
            for m in raw["migrations"]
        ]
        return cls(
            database=database,
            url=url,
            dialect=dialect,
            table=table,
            migrations=migrations,
            ahead=list(raw["ahead"]),
            inheritable=list(raw.get("inheritable", ())),
            refusal=refusal if refusal is not None else raw["refusal"],
            refusal_needs_attention=(
                True if refusal is not None else raw["refusal_needs_attention"]
            ),
        )

    def render(self, steps: bool = False) -> str:
        """Render the text `ferro migrate status` prints.

        Args:
            steps: Print every migration's steps, each with its class after
                its flags, as `--steps` does. Defaults to only the
                migrations that need attention.

        Returns:
            The report as plain text, one line per migration or step.
        """
        lines = [f"{self.database} ({self.dialect}) · {self.table}", ""]
        names = [m.name for m in self.migrations] + self.ahead
        name_width = max((len(name) for name in names), default=0)
        expanded = [m for m in self.migrations if steps or m.expands]
        file_width = max((len(s.file) for m in expanded for s in m.steps), default=0)
        for migration in self.migrations:
            hint = f"  {migration.hint}" if migration.hint else ""
            lines.append(f"{migration.name:<{name_width}}  {migration.state}{hint}")
            if migration not in expanded:
                continue
            for step in migration.steps:
                flags = "".join(f"  [{flag}]" for flag in step.flags)
                shown_class = f"  {step.step_class}" if steps else ""
                lines.append(
                    f"  {step.file:<{file_width}}  {step.shown}{flags}{shown_class}"
                )
                if step.state == "applied (different checksum)":
                    lines.append(f"    applied   sha384:{step.applied_checksum}")
                    lines.append(f"    on disk   sha384:{step.on_disk_checksum}")
                elif step.error and step.state in ("failed", "running", "reverting"):
                    lines.extend(f"    {line}" for line in step.error.splitlines())
        for name in self.ahead:
            lines.append(f"{name:<{name_width}}  applied, not in the directory")
        if self.refusal is not None:
            lines += ["", self.refusal]
        return "\n".join(lines)

    def to_json(self) -> str:
        """Render the report as the JSON document `status --json` prints.

        Returns:
            The report as indented JSON.
        """
        return json.dumps(
            {
                "database": self.database,
                "url": self.url,
                "dialect": self.dialect,
                "table": self.table,
                "pending": self.pending,
                "needs_attention": self.needs_attention,
                "exit_code": self.exit_code,
                "migrations": [
                    {
                        "number": m.number,
                        "name": m.name,
                        "state": m.state,
                        "applied_at": m.applied_at,
                        "steps": [asdict(s) for s in m.steps],
                    }
                    for m in self.migrations
                ],
                "ahead": self.ahead,
                "inheritable": self.inheritable,
                "refusal": self.refusal,
            },
            indent=2,
        )
