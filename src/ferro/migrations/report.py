"""What ``ferro migrate status`` reports, and how it prints (#466, #473).

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
where something needs attention, or everywhere with ``--steps``. Every state
is decided in the Rust core (``_core._run_status``); this module only names
and prints them (a chunked step's ``rows_done`` comes from its record). ``status`` exits 3 when anything is pending and 4 when
anything needs attention (a failed, interrupted or reverting step, an edited
file, a database ahead of the checkout, or a refusal ``up`` would meet).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import MigrationRefused

__all__ = [
    "MigrationStatus",
    "RunRefused",
    "StatusReport",
    "StepStatus",
]


class RunRefused(MigrationRefused):
    """A migration run refused before running anything, naming the fix.

    Raised by the Rust core for every refusal the run planner, the run lock
    and the tracking tables make; its message is the operator's text. A
    planner refusal also carries what it is about, so a caller matches on
    ``kind`` rather than on the text: ``kind`` (``"irreversible"``,
    ``"edited_applied"``, ``"edited_chunked"``, ...), ``migration`` and
    ``step`` (numbers, when it names them) and ``reason`` (an irreversible
    step's declared reason). Each is ``None`` for a refusal that is not the
    planner's (a lost lock, a missing schema).
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str | None = None,
        migration: int | None = None,
        step: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.migration = migration
        self.step = step
        self.reason = reason


_WORDS = {
    "applied": "applied",
    "applied_different_checksum": "applied (different checksum)",
    "applied_baseline": "applied (baseline)",
    "pending": "pending",
    "running": "running",
    "failed": "failed",
    "interrupted": "interrupted",
    "reverting": "reverting",
}
_ATTENTION = {"applied (different checksum)", "failed", "interrupted", "reverting"}
_NOT_PENDING = {"applied", "applied (different checksum)", "applied (baseline)"}
_WHOLE = {"applied", "applied (baseline)", "pending"}


@dataclass(frozen=True)
class StepStatus:
    """One step of one migration."""

    step: int
    file: str
    """The file this database executes (or executed)."""
    state: str
    """``applied``, ``applied (different checksum)``, ``applied
    (baseline)``, ``pending``, ``running``, ``failed``, ``interrupted`` or
    ``reverting``."""
    error: str | None = None
    """The last attempt's error, when recorded."""
    applied_checksum: str | None = None
    on_disk_checksum: str | None = None
    flags: list[str] = field(default_factory=list)
    """``destructive`` / ``data-dependent`` headers the file carries."""
    rows_done: int | None = None
    """The rows a chunked step's current walk has committed."""

    @property
    def shown(self) -> str:
        """The state as ``status`` prints it: a failed chunked step says how
        far it got (``failed at 30,000 rows``)."""
        if self.state == "failed" and self.rows_done is not None:
            return f"failed at {self.rows_done:,} rows"
        return self.state

    @property
    def needs_attention(self) -> bool:
        return self.state in _ATTENTION

    @property
    def pending(self) -> bool:
        return self.state not in _NOT_PENDING


@dataclass(frozen=True)
class MigrationStatus:
    """One migration and its steps."""

    number: int
    name: str
    """``NNNN_<name>``."""
    steps: list[StepStatus]

    @property
    def state(self) -> str:
        """``applied`` / ``applied (baseline)`` / ``pending`` when every
        step agrees; ``running`` while a run is in it; ``applied (different
        checksum)`` when every step is applied and one changed; else
        ``partial, X of N steps``."""
        words = {step.state for step in self.steps}
        if len(words) == 1 and next(iter(words)) in _WHOLE:
            return next(iter(words))
        if "running" in words:
            return "running"
        if words <= _NOT_PENDING:
            return "applied (different checksum)"
        done = sum(1 for step in self.steps if not step.pending)
        return f"partial, {done} of {len(self.steps)} steps"

    @property
    def expands(self) -> bool:
        """Whether ``status`` prints this migration's steps without ``--steps``."""
        return self.state not in _WHOLE


@dataclass(frozen=True)
class StatusReport:
    """What ``ferro migrate status`` prints, and what ``status()`` returns."""

    database: str
    dialect: str
    table: str
    """``<schema>._ferro_migrations``."""
    migrations: list[MigrationStatus]
    ahead: list[str] = field(default_factory=list)
    """Migrations this database has applied that the directory lacks."""
    refusal: str | None = None
    """What ``up`` would refuse with, when it would."""
    refusal_needs_attention: bool = False

    @property
    def pending(self) -> bool:
        """Anything still to apply."""
        return any(step.pending for m in self.migrations for step in m.steps)

    @property
    def needs_attention(self) -> bool:
        """A person has to look before anything runs."""
        return (
            bool(self.ahead)
            or (self.refusal is not None and self.refusal_needs_attention)
            or any(step.needs_attention for m in self.migrations for step in m.steps)
        )

    @property
    def exit_code(self) -> int:
        """4 when anything needs attention, 3 when anything is pending, else 0."""
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
        dialect: str,
        table: str,
        refusal: str | None = None,
        rows_done: dict[tuple[int, int], int] | None = None,
    ) -> StatusReport:
        """Build the report from ``_core._run_status``'s document; ``refusal``
        (the newer-format text) replaces the planner's when given;
        ``rows_done`` holds each chunked step's committed rows by
        ``(migration, step)``."""
        rows_done = rows_done or {}
        migrations = [
            MigrationStatus(
                number=m["number"],
                name=m["name"],
                steps=[
                    StepStatus(
                        step=s["step"],
                        file=s["file"],
                        state=_WORDS[s["state"]],
                        error=s["error"],
                        applied_checksum=s["applied_checksum"],
                        on_disk_checksum=s["on_disk_checksum"],
                        flags=list(s["flags"]),
                        rows_done=rows_done.get((m["number"], s["step"])),
                    )
                    for s in m["steps"]
                ],
            )
            for m in raw["migrations"]
        ]
        return cls(
            database=database,
            dialect=dialect,
            table=table,
            migrations=migrations,
            ahead=list(raw["ahead"]),
            refusal=refusal if refusal is not None else raw["refusal"],
            refusal_needs_attention=(
                True if refusal is not None else raw["refusal_needs_attention"]
            ),
        )

    def render(self, steps: bool = False) -> str:
        """The text ``ferro migrate status`` prints (``--steps``: every
        migration's steps)."""
        lines = [f"{self.database} ({self.dialect}) · {self.table}", ""]
        names = [m.name for m in self.migrations] + self.ahead
        name_width = max((len(name) for name in names), default=0)
        expanded = [m for m in self.migrations if steps or m.expands]
        file_width = max((len(s.file) for m in expanded for s in m.steps), default=0)
        for migration in self.migrations:
            lines.append(f"{migration.name:<{name_width}}  {migration.state}")
            if migration not in expanded:
                continue
            for step in migration.steps:
                flags = "".join(f"  [{flag}]" for flag in step.flags)
                lines.append(f"  {step.file:<{file_width}}  {step.shown}{flags}")
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
        """The same report as a JSON document (``status --json``)."""
        return json.dumps(
            {
                "database": self.database,
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
                        "steps": [asdict(s) for s in m.steps],
                    }
                    for m in self.migrations
                ],
                "ahead": self.ahead,
                "refusal": self.refusal,
            },
            indent=2,
        )
