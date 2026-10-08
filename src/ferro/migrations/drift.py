"""``ferro migrate drift``: the live database against its last applied snapshot.

```text
$ ferro migrate drift
drift against 0007_nickname:
  user.nickname column is missing
  idx_user_email index is invalid
  ck_order_total check body differs
$ ferro migrate drift        # clean
no drift against 0007_nickname
```

Drift is what the one planner would change to turn the live database into the
schema snapshot of the last migration applied to it (ADR-0023). Nothing here
compares live and declared state: the live database is read into the planner's
input (``_core._live_schema_ir``, with the live facts beside it), planned
against the snapshot (``_core._plan_from_ir``, destructive changes on, so
extra objects are reported too), and each planned op is printed as one line
(:func:`render_op`). That comparison of the live database with one snapshot
is :func:`against`; ``baseline`` makes the same call with its target's
snapshot, so the two check exactly the same things.

Only the snapshot's tables are read, so a live table the snapshot does not
declare (``alembic_version``, an extension's tables, the tracking tables) is
never drift (ADR-0031). A database mid-migration (a ``failed``, ``running``,
``interrupted`` or ``reverting`` step, or a run holding the lock) is at
neither snapshot, so drift refuses rather than report the run's own partial
work (ADR-0043); so does a database with no migration records, which has no
applied snapshot to drift from. ``drift`` takes no lock and writes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import _core
from . import runner
from .errors import MigrationRefused
from .report import RunRefused, StatusReport
from .target import Target

if TYPE_CHECKING:
    from ..settings import DatabaseSettings, FerroSettings

__all__ = ["DriftReport", "against", "drift", "render_op"]

_DESTRUCTIVE = json.dumps({"destructive": True})


@dataclass(frozen=True)
class DriftReport:
    """What ``drift`` found.

    ``lines`` holds one plain-language line per planned op; ``operations``
    the planner's ops themselves (``{"kind": ..., <fields>}``). ``refusal``
    is why there was nothing to compare: a run is unfinished, or the
    database has no migration records. Either way ``clean`` is false.
    """

    against: str | None
    """The applied migration whose snapshot the database was compared with
    (``NNNN_<name>``), or ``None`` when drift refused."""
    lines: list[str] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    refusal: str | None = None
    reports: list[dict[str, Any]] = field(default_factory=list)
    """What the planner reports without planning an op for it (a foreign
    or unverifiable row policy, ...), each ``{"kind", "subject", "text",
    "recurs", "blocks"}``, in planning order. Never makes the report
    unclean."""

    @property
    def warnings(self) -> list[str]:
        """Each report's sentence, in order."""
        return [report["text"] for report in self.reports]

    @property
    def clean(self) -> bool:
        """The database matches the snapshot it was compared with."""
        return self.refusal is None and not self.lines

    def render(self) -> str:
        """The text ``ferro migrate drift`` prints on stdout (empty for a refusal)."""
        if self.refusal is not None:
            return ""
        if not self.lines:
            return f"no drift against {self.against}"
        return "\n".join(
            [f"drift against {self.against}:"] + [f"  {line}" for line in self.lines]
        )

    def raise_for_problems(self) -> None:
        """Raise :class:`MigrationRefused` naming every line (or the refusal)
        unless the report is clean."""
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
    "RenamePolicy": lambda op: f"{op['old']} policy is named {op['new']} in the snapshot",
    "AddColumn": lambda op: f"{_column(op)} column is missing",
    "DropColumn": lambda op: f"{_column(op)} column is extra",
    "AlterColumnType": _type,
    "AlterColumnNullability": _nullability,
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


def render_op(op: dict[str, Any]) -> str:
    """One planner op as one plain-language line (``team.name column is
    missing``). An op kind this module does not know still renders, as
    ``<kind> on <table>``; a known kind missing one of its fields raises
    ``ValueError`` naming both.

    ``AlterColumnType`` reads ``live_type`` / ``snapshot_type`` and
    ``AlterColumnNullability`` reads ``live_nullable`` when the op carries
    them (:func:`against` adds them); without them the line says only that the
    two differ.
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


def _indexes(envelope: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Every standalone index of ``envelope`` by ``(table, name)``, as
    ``{"columns", "unique"}``: a declared unique sits in ``uniques``, a live
    one in ``indexes``."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for model in envelope["payload"]["models"]:
        table = model["table_name"]
        for index in model.get("indexes") or []:
            out[(table, index["name"])] = {
                "columns": list(index["columns"]),
                "unique": bool(index.get("unique")),
            }
        for unique in model.get("uniques") or []:
            out[(table, unique["name"])] = {
                "columns": list(unique["columns"]),
                "unique": True,
            }
    return out


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
        for op in operations
    ):
        return operations
    live_columns, snapshot_columns = _columns(live), _columns(snapshot)
    live_indexes, snapshot_indexes = _indexes(live), _indexes(snapshot)
    described = []
    for op in operations:
        if op["kind"] == "RedefineIndex":
            index = (op["table"], op["name"])
            if index in live_indexes and index in snapshot_indexes:
                op = {
                    **op,
                    "live_index": live_indexes[index],
                    "snapshot_index": snapshot_indexes[index],
                }
            described.append(op)
            continue
        key = (op.get("table"), op.get("column"))
        before, after = live_columns.get(key), snapshot_columns.get(key)
        if before is not None and after is not None:
            if op["kind"] == "AlterColumnType":
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
    """Drift of the live database on open connection ``using`` against one
    schema snapshot (the ``["snapshot"]["ir"]`` of a migration in the
    directory), reported as against ``migration`` (``NNNN_<name>``).

    The check :func:`drift` makes against the last applied migration and
    ``baseline`` makes against its target, and nothing else: only the
    snapshot's tables are read (ADR-0031), destructive changes count, each
    planned op is one :func:`render_op` line, and nothing is refused, locked
    or written::

        report = await against(snapshot, migration="0007_nickname", using="default")
        report.lines   # ["user.nickname column is missing"]

    Raises:
        MigrationRefused: ``using`` is not an open connection.
    """
    dialect = _core.connection_backend(using)
    if dialect is None:
        raise MigrationRefused(f"connection `{using}` is not open; connect it first")
    # The snapshot's live tables and the old table of each live rename hint
    # (ADR-0032), read by the reconciliation pass's own rule (ADR-0047).
    live_json, facts_json = await _core._live_schema_ir(using, json.dumps(snapshot))
    plan = json.loads(
        _core._plan_from_ir(
            live_json,
            json.dumps(snapshot),
            dialect,
            _DESTRUCTIVE,
            False,
            facts_json,
        )
    )
    operations = _describe(plan["operations"], json.loads(live_json), snapshot, dialect)
    return DriftReport(
        against=migration,
        lines=[render_op(op) for op in operations],
        operations=operations,
        reports=list(plan["reports"]),
    )


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
    """Compare the live database with the schema snapshot of the last
    migration applied to it (``ferro migrate drift``). Takes no lock and
    changes nothing.

    Works on ``using`` (an open connection), on a private connection to
    ``url``, or on the default connection. Raises nothing for drift or for
    a database it cannot compare (mid-migration, no records): the report
    says so, and ``.raise_for_problems()`` raises :class:`MigrationRefused`
    with every line, so a fixture aborts in one line::

        (await ferro.migrations.drift()).raise_for_problems()

    Raises:
        MigrationRefused: the configuration names no single database, there
            is no connection to work on, or the migrations directory is
            unreadable.
    """
    target = Target.resolve(settings, database, using=using, url=url)
    async with target.open() as name:
        return await _audit(name, target.database)
