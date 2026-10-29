"""`ferro migrate drift`: the live database against its last applied snapshot.

```text
$ ferro migrate drift
drift against 0007_nickname:
  user.nickname column is missing
  idx_user_email index is invalid
  ck_order_total check body differs
$ ferro migrate drift        # clean
no drift against 0007_nickname
```

Drift is what the planner would change to turn the live database into the
schema snapshot of the last migration applied to it. Nothing here compares
live and declared state: one core call reads the snapshot's tables from the
live database the way the auto-migrate pass reads them, and plans them
against the snapshot with destructive changes on, so extra objects are
reported too; each planned op is printed as one line (`render_op`). That
comparison of the live database with one snapshot is `against`; `baseline`
makes the same call with its target's snapshot, so the two check exactly
the same things.

Only the snapshot's tables are read, so a live table the snapshot does not
declare (`alembic_version`, an extension's tables, the tracking tables) is
never drift. A database mid-migration (a `failed`, `running`, `interrupted`
or `reverting` step, or a run holding the lock) is at neither snapshot, so
drift refuses rather than report the run's own partial work; so does a
database with no migration records, which has no applied snapshot to drift
from. `drift` takes no lock and writes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import _core
from ..exceptions import OperationalError
from . import runner
from .errors import MigrationRefused
from .report import RunRefused, StatusReport
from .target import Target

if TYPE_CHECKING:
    from ..settings import DatabaseSettings, FerroSettings

__all__ = ["DriftReport", "against", "drift", "render_op"]


@dataclass(frozen=True)
class DriftReport:
    """What [`drift`][ferro.migrations.drift] found: one line per difference, or why it could not compare."""

    against: str | None
    """The applied migration the database was compared with (`NNNN_<name>`), or `None` when drift refused."""
    lines: list[str] = field(default_factory=list)
    """One plain-language line per difference, such as `author.slug column is missing`."""
    operations: list[dict[str, Any]] = field(default_factory=list)
    """The planner's ops behind the lines, each `{"kind": ..., <fields>}`."""
    refusal: str | None = None
    """Why there was nothing to compare: a run is unfinished, or the database has no migration records."""
    reports: list[dict[str, Any]] = field(default_factory=list)
    """What the planner reports without planning an op for it, in planning order.

    A foreign or unverifiable row policy, for example; each is
    `{"kind", "subject", "text", "recurs", "blocks"}`. A report never makes
    the drift report unclean.
    """

    @property
    def warnings(self) -> list[str]:
        """Each report's sentence, in order."""
        return [report["text"] for report in self.reports]

    @property
    def clean(self) -> bool:
        """Whether the database matches the snapshot it was compared with; false after a refusal."""
        return self.refusal is None and not self.lines

    def render(self) -> str:
        """Render the text `ferro migrate drift` prints on stdout.

        Returns:
            The drift lines under `drift against <migration>:`, `no drift
                against <migration>`, or an empty string for a refusal.
        """
        if self.refusal is not None:
            return ""
        if not self.lines:
            return f"no drift against {self.against}"
        return "\n".join(
            [f"drift against {self.against}:"] + [f"  {line}" for line in self.lines]
        )

    def raise_for_problems(self) -> None:
        """Raise unless the report is clean.

        Raises:
            MigrationRefused: The report has drift lines (the message names
                every one) or a refusal (the message is the refusal).
        """
        if self.refusal is not None:
            raise MigrationRefused(self.refusal)
        if self.lines:
            raise MigrationRefused(self.render())


# -- the lines ---------------------------------------------------------------------


def _column(op: dict[str, Any]) -> str:
    return f"{op['table']}.{op['column']}"


def _type(op: dict[str, Any]) -> str:
    live, declared = op.get("live_type"), op.get("snapshot_type")
    if live is None or declared is None:
        return f"{_column(op)} has a different type than the snapshot"
    return f"{_column(op)} has type {live}, snapshot says {declared}"


def _nullability(op: dict[str, Any]) -> str:
    live = op.get("live_nullable")
    if live is None:
        return f"{_column(op)} nullability differs from the snapshot"
    if live:
        return f"{_column(op)} is nullable, snapshot says NOT NULL"
    return f"{_column(op)} is NOT NULL, snapshot says nullable"


def _constraint_noun(name: str) -> str:
    if name.startswith("fk_"):
        return "foreign key"
    if name.startswith("ck_"):
        return "check"
    return "constraint"


def _unique(unique: bool) -> str:
    return "unique" if unique else "not unique"


def _redefined(op: dict[str, Any]) -> str:
    live, declared = op.get("live_index"), op.get("snapshot_index")
    if live is None or declared is None:
        return f"{op['name']} index is defined differently than the snapshot"
    if live["columns"] != declared["columns"]:
        return (
            f"{op['name']} index is on ({', '.join(live['columns'])}), "
            f"snapshot says ({', '.join(declared['columns'])})"
        )
    return (
        f"{op['name']} index is {_unique(live['unique'])}, "
        f"snapshot says {_unique(declared['unique'])}"
    )


_RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "AddEnumLabel": lambda op: (
        f"{op['type_name']} enum type is missing label {op['label']}"
    ),
    "CreateEnumType": lambda op: f"{op['type_name']} enum type is missing",
    "DropEnumType": lambda op: f"{op['type_name']} enum type is extra",
    "RenameEnumLabel": lambda op: (
        f"{op['type_name']} enum label {op['old']} is named {op['new']} in the snapshot"
    ),
    # Planned only between two snapshots; drift's live side never meets it.
    "RemoveEnumLabel": lambda op: (
        f"{op['type_name']} enum type has label {op['label']} the snapshot removes"
    ),
    "RenameEnumType": lambda op: (
        f"{op['old']} enum type is named {op['new']} in the snapshot"
    ),
    "AddTable": lambda op: f"{op['table']} table is missing",
    "DropTable": lambda op: f"{op['table']} table is extra",
    "RenameTable": lambda op: f"{op['old']} table is named {op['new']} in the snapshot",
    "RenameColumn": lambda op: (
        f"{op['table']}.{op['old']} column is named {op['new']} in the snapshot"
    ),
    "RenameIndex": lambda op: f"{op['old']} index is named {op['new']} in the snapshot",
    "RenameConstraint": lambda op: (
        f"{op['old']} {_constraint_noun(op['old'])} is named {op['new']} in the snapshot"
    ),
    "RenamePolicy": lambda op: (
        f"{op['old']} policy is named {op['new']} in the snapshot"
    ),
    "AddColumn": lambda op: f"{_column(op)} column is missing",
    "DropColumn": lambda op: f"{_column(op)} column is extra",
    "AlterColumnType": _type,
    "AlterColumnNullability": _nullability,
    "AlterGeneratedExpression": lambda op: (
        f"{_column(op)} generated expression differs"
    ),
    "DropGeneratedExpression": lambda op: (
        f"{_column(op)} is generated in the database, plain in the snapshot"
    ),
    "DropDefault": lambda op: (
        f"{_column(op)} has default {op['was']} in the database, none declared"
    ),
    "ChangePrimaryKey": lambda op: (
        f"{op['table']} primary key is ({', '.join(op['from'])}), "
        f"snapshot says ({', '.join(op['to'])})"
    ),
    "AddIndex": lambda op: f"{op['name']} index is missing",
    "DropIndex": lambda op: f"{op['name']} index is extra",
    "RedefineIndex": lambda op: _redefined(op),
    "RebuildIndex": lambda op: f"{op['name']} index is invalid",
    "AddForeignKey": lambda op: f"{_column(op)} foreign key is missing",
    "DropForeignKey": lambda op: f"{op['name']} foreign key is extra",
    "RebuildForeignKey": lambda op: (
        f"{op['old_name']} foreign key differs from the snapshot"
    ),
    "AddCheck": lambda op: f"{op['name']} check is missing",
    "RebuildCheck": lambda op: f"{op['name']} check body differs",
    "DropCheck": lambda op: f"{op['name']} check is extra",
    "ValidateConstraint": lambda op: (
        f"{op['name']} {_constraint_noun(op['name'])} is not validated"
    ),
    "AddRowPolicy": lambda op: f"{op['name']} policy is missing",
    "RebuildRowPolicy": lambda op: f"{op['name']} policy body differs",
    "DropRowPolicy": lambda op: f"{op['name']} policy is extra",
    "EnableRowSecurity": lambda op: f"{op['table']} row security is not enabled",
    "ForceRowSecurity": lambda op: f"{op['table']} row security is not forced",
    "DisableRowSecurity": lambda op: (
        f"{op['table']} row security is enabled, snapshot declares none"
    ),
    "NoForceRowSecurity": lambda op: (
        f"{op['table']} row security is forced, snapshot does not force it"
    ),
}
"""One renderer per ``MigrationOp`` variant; a test walks the Rust enum so a
new variant without a line here fails."""


def _carried_line(noun: str) -> Callable[[dict[str, Any], str], str]:
    def line(op: dict[str, Any], carried: str) -> str:
        if carried == "changed":
            return f"{op['name']} {noun} body differs"
        return f"{op['name']} {noun} not compared while its column changes type"

    return line


_CARRIED: dict[str, Callable[[dict[str, Any], str], str]] = {
    "AddCheck": _carried_line("check"),
    "AddRowPolicy": _carried_line("policy"),
}
"""The add back of a carried artifact, which says what the pair changes."""

_HELD: dict[str, Callable[[dict[str, Any], str], str]] = {
    "check": lambda op, name: (
        f"{name} check not compared while its column's type change is withheld"
    ),
    "generated_column": lambda op, name: (
        f"{op['table']}.{name} generated expression not compared while its "
        "input's type change is withheld"
    ),
    "policy": lambda op, name: (
        f"{name} policy not compared while its column's type change is withheld"
    ),
}
"""What a type change no door runs holds (``held``), each left standing and
not compared until the change runs: one line each, after the type line,
ending with the fix that makes the change run."""


def _held_lines(op: dict[str, Any]) -> list[str]:
    held = op["held"]
    fix = f'resolve the type change of "{op["column"]}" first: {held["fix"]}'
    return [
        f"{_HELD[artifact['kind']](op, artifact['name'])}; {fix}"
        for artifact in held["artifacts"]
    ]


def render_op(op: dict[str, Any]) -> str:
    """Render one planner op as one plain-language line.

    An op kind this function does not know still renders, as
    `<kind> on <table>`. `AlterColumnType` reads `live_type` /
    `snapshot_type` and `AlterColumnNullability` reads `live_nullable` when
    the op carries them; without them the line says only that the two
    differ.

    Args:
        op: The op, as `DriftReport.operations` holds it:
            `{"kind": ..., <fields>}`.

    Returns:
        The line, such as `author.slug column is missing`.

    Raises:
        ValueError: A known kind is missing one of its fields; the message
            names both.

    Examples:
        ```python {test=run prelude=reference}
        from ferro.migrations import render_op

        print(render_op({"kind": "AddColumn", "table": "author", "column": "slug"}))
        # author.slug column is missing
        print(render_op({"kind": "DropTable", "table": "post"}))
        # post table is extra
        ```
    """
    kind = op.get("kind", "unknown op")
    renderer = _RENDERERS.get(kind)
    if renderer is not None:
        try:
            return renderer(op)
        except KeyError as missing:
            raise ValueError(
                f"{kind} op has no {missing.args[0]!r} field; the planner's op "
                f"and this renderer disagree on its shape"
            ) from None
    return f"{kind} on {op['table']}" if op.get("table") else kind


# -- the audit -----------------------------------------------------------------------


def _storage(column: dict[str, Any], dialect: str) -> str:
    """A column's storage type as the one storage decision names it."""
    resolved = json.loads(_core._resolve_storage_type(json.dumps(column), dialect))
    if resolved["kind"] == "pg_enum":
        return f"enum {resolved['name']}"
    return resolved["token"]


def _columns(envelope: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (model["table_name"], column["name"]): column
        for model in envelope["payload"]["models"]
        for column in model["columns"]
    }


def _index(envelope: dict[str, Any], table: str, name: str) -> dict[str, Any] | None:
    """The standalone index ``name`` of ``envelope`` on ``table``, as
    ``{"columns", "unique"}``: the core's one reading of it
    (``_declared_index``), whether a snapshot's or a live read's."""
    found = _core._declared_index(json.dumps(envelope), table, name)
    if found is None:
        return None
    columns, unique = found
    return {"columns": columns, "unique": unique}


def _describe(
    operations: list[dict[str, Any]],
    live: dict[str, Any],
    snapshot: dict[str, Any],
    dialect: str,
) -> list[dict[str, Any]]:
    """The planner's ops with the two sides of a column change or of an index
    redefinition attached, read from the two envelopes the planner
    compared."""
    if not any(
        op["kind"] in ("AlterColumnType", "AlterColumnNullability", "RedefineIndex")
        or op.get("replaces") == "own_type"
        for op in operations
    ):
        return operations
    live_columns, snapshot_columns = _columns(live), _columns(snapshot)
    described = []
    for op in operations:
        if op["kind"] == "RedefineIndex":
            live_index = _index(live, op["table"], op["name"])
            snapshot_index = _index(snapshot, op["table"], op["name"])
            if live_index is not None and snapshot_index is not None:
                op = {
                    **op,
                    "live_index": live_index,
                    "snapshot_index": snapshot_index,
                }
            described.append(op)
            continue
        key = (op.get("table"), op.get("column"))
        before, after = live_columns.get(key), snapshot_columns.get(key)
        if before is not None and after is not None:
            # A generated column whose own type changes is dropped and
            # added back: its add says the type.
            if op["kind"] == "AlterColumnType" or op.get("replaces") == "own_type":
                op = {
                    **op,
                    "live_type": _storage(before, dialect),
                    "snapshot_type": _storage(after, dialect),
                }
            elif op["kind"] == "AlterColumnNullability":
                op = {
                    **op,
                    "live_nullable": before["nullable"],
                    "snapshot_nullable": after["nullable"],
                }
        described.append(op)
    return described


def _mid_run(status: StatusReport) -> str | None:
    """The first step a run left unfinished, as ``<migration>/<file> is
    <state>``, or a migration only partly applied."""
    migration = status.unfinished
    if migration is None:
        return None
    step = migration.unfinished_step
    if step is not None:
        return f"{migration.name}/{step.file} is {step.state}"
    return f"{migration.name} is partly applied"


def _refused(why: str) -> DriftReport:
    return DriftReport(against=None, refusal=why)


async def against(
    snapshot: dict[str, Any], *, migration: str, using: str
) -> DriftReport:
    """Compare the live database with one schema snapshot.

    This is the check `drift` makes against the last applied migration and
    `baseline` makes against its target, and nothing else: only the
    snapshot's tables are read, destructive changes count, each planned op
    is one `render_op` line, and nothing is refused, locked or written.

    Args:
        snapshot: The schema snapshot, the `["snapshot"]["ir"]` of a
            migration in the directory.
        migration: The migration the report says it compared against
            (`NNNN_<name>`).
        using: The open connection to read.

    Returns:
        The drift report.

    Raises:
        MigrationRefused: `using` is not an open connection, or on Postgres
            the role cannot read a table the snapshot holds (the message
            names the `GRANT SELECT` it needs).

    Examples:
        ```python {test=compile reason="needs a migrations project and a database"}
        report = await against(snapshot, migration="0007_nickname", using="default")
        report.lines  # ["user.nickname column is missing"]
        ```
    """
    if _core.connection_backend(using) is None:
        raise MigrationRefused(f"connection `{using}` is not open; connect it first")
    # One core call reads the snapshot's live tables (and the old table of
    # each live rename hint, ADR-0032) by the pass's own rule (ADR-0047) and
    # plans them toward the snapshot, destructive changes on.
    try:
        plan = await _core._plan_drift(using, json.dumps(snapshot))
    except OperationalError as err:
        # The role cannot read a table drift compares; the message names the
        # GRANT.
        if err.sqlstate == "42501":
            raise MigrationRefused(str(err)) from None
        raise
    operations = _describe(plan["operations"], plan["live"], snapshot, plan["dialect"])
    return DriftReport(
        against=migration,
        lines=_lines(operations),
        operations=operations,
        reports=list(plan["reports"]),
    )


def _lines(operations: list[dict[str, Any]]) -> list[str]:
    """One line per op, a generated column the plan drops and adds back read
    as one difference, by the reason the planner gives the pair
    (``replaces``): its type differs, or the live column is not the stored
    generated one the snapshot declares (its expression differs). One taken
    down only around a type change of a column it reads has no line: that
    column's own line says it. A policy or check the planner takes down and
    puts back around such a change (``carried``) has a line, on its add back,
    only when what it puts back is not what the table holds: its body
    differs, or it could not be compared while its column changes type. A
    type change no door runs names, after its own line, each check,
    generated column and policy over the column it holds (``held``): none
    can be compared until the change runs."""
    lines = []
    for op in operations:
        if op.get("held"):
            lines.append(render_op(op))
            lines.extend(_held_lines(op))
            continue
        replaces = op.get("replaces")
        carried = op.get("carried")
        if carried is not None:
            if carried != "unchanged" and op["kind"] in _CARRIED:
                lines.append(_CARRIED[op["kind"]](op, carried))
            continue
        if replaces is None:
            lines.append(render_op(op))
        elif op["kind"] != "AddColumn" or replaces == "input_type":
            continue
        elif replaces == "own_type":
            lines.append(_type(op))
        else:
            lines.append(f"{_column(op)} generated expression differs")
    return lines


async def _audit(name: str, database: DatabaseSettings) -> DriftReport:
    """Drift on open connection ``name``: refuses, or plans the live database
    against the last applied snapshot."""
    tracked = await runner.open_tracked(name, database)
    if tracked.refusal is not None:
        return _refused(tracked.refusal)
    if not tracked.records:
        return _refused(
            "ferro migrate drift: this database has no migration records, so "
            "there is no applied snapshot to compare it with. Run `ferro migrate "
            "baseline` to record a schema it already has, or `ferro migrate up` "
            "to build it."
        )
    held = await tracked.lock_held()
    try:
        contents = tracked.migrations
    except RunRefused as err:
        raise MigrationRefused(str(err)) from None
    status = StatusReport.from_core(
        tracked.status(lock_held=held),
        database=database.name,
        url=tracked.url,
        dialect=tracked.dialect,
        table=tracked.tracking_table,
    )
    mid_run = _mid_run(status) or (
        "a migration run holds the run lock" if held else None
    )
    if mid_run is not None:
        return _refused(
            f"ferro migrate drift: {mid_run}. Drift is measured against the last "
            f"applied migration, and a database mid-migration is at neither that "
            f"migration nor the next. Run `ferro migrate status` to see where it "
            f"stands; finish the migration with `ferro migrate up` or revert it "
            f"with `ferro migrate down`, then check drift again."
        )
    if status.ahead:
        return _refused(
            f"ferro migrate drift: this database has applied "
            f"{', '.join(status.ahead)}, which this checkout does not have, so "
            f"its last applied snapshot is not here to compare with. Run `ferro "
            f"migrate status` to see them, and check drift from a checkout that "
            f"has them."
        )
    head = status.head_applied
    # Records exist, so a migration is applied or unfinished.
    if head is None:  # pragma: no cover
        return _refused(
            "ferro migrate drift: no migration is fully applied to this database. "
            "Run `ferro migrate status` to see where it stands."
        )
    snapshots = {m["number"]: m["snapshot"]["ir"] for m in contents["migrations"]}
    return await against(snapshots[head.number], migration=head.name, using=name)


async def drift(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    using: str | None = None,
    url: str | None = None,
) -> DriftReport:
    """Compare the live database with the schema snapshot of the last migration applied to it.

    This is `ferro migrate drift`. It takes no lock and changes nothing, and
    raises nothing for drift or for a database it cannot compare
    (mid-migration, no records): the report says so.

    Args:
        settings: The project's configuration. Defaults to `FerroSettings()`.
        database: The configured database. Defaults to the only one
            configured.
        using: An open connection to work on. Defaults to the default
            connection.
        url: A URL to open a private connection to, closed after the call.
            Refused together with `using`.

    Returns:
        The drift report; its `raise_for_problems()` raises with every line.

    Raises:
        MigrationRefused: The configuration names no single database, there
            is no connection to work on, or the migrations directory is
            unreadable.

    Examples:
        A test fixture that aborts on drift, in one line:

        ```python {test=compile reason="needs a migrations project and a database"}
        (await ferro.migrations.drift()).raise_for_problems()
        ```
    """
    target = Target.resolve(settings, database, using=using, url=url)
    async with target.open() as name:
        return await _audit(name, target.database)
