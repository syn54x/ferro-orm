"""The Alembic bridge's translator: the one planner's ops as Alembic ops (ADR-0041).

```python
class Card(Model):
    flavor: str | None = None                      # new
    __ferro_checks__ = (Check("flavor_set", lambda card: card.flavor != None),)
```

```python
def upgrade():
    op.add_column('card', sa.Column('flavor', sa.String(), nullable=True))
    op.execute(sa.DDL('ALTER TABLE "card" ADD CONSTRAINT "ck_card_flavor_set" CHECK (...)'))

def downgrade():
    op.execute(sa.DDL('ALTER TABLE "card" DROP CONSTRAINT "ck_card_flavor_set"'))
    op.drop_column('card', 'flavor')
```

Every op comes from the planner (``_core._plan_from_ir`` for ``upgrade()``,
``_core._plan_reverse_from_ir`` for ``downgrade()``), in the planner's order;
this module only says how each one is written. Where Alembic has an op of its
own (a table, a column, its type and nullability, an index, a foreign key, a
table or column rename) the revision uses it, built from the same
``sa.Column`` the bridge's ``get_metadata()`` builds. Everything else runs the
statement the reconciliation pass would run, byte for byte, as
``op.execute(sa.DDL(...))``. A change that needs values of existing rows is
the plain op under ``# ferro: data-dependent``; one that drops data sits under
``# ferro: destructive``; a step the planner calls irreversible renders as
``raise RuntimeError("<reason>")``.
"""

from __future__ import annotations

from typing import Any, Literal

import sqlalchemy as sa
from alembic.autogenerate import renderers
from alembic.autogenerate.render import render_op
from alembic.operations import ops

__all__ = [
    "FerroExecuteOp",
    "FerroIrreversibleOp",
    "FerroMarkedOp",
    "FerroRenameTableOp",
    "FerroRevisionOps",
    "FerroWarningOp",
    "translate",
]

Direction = Literal["up", "down"]


class FerroExecuteOp(ops.MigrateOperation):
    """One statement the planner rendered, run as written.

    Renders as ``op.execute(sa.DDL('<statement>'))``: a DDL construct is never
    parsed for bind parameters (an enum label ``':admin'`` would be one in
    ``text()``, #449), and its one rule is that ``%`` is written ``%%``. With
    ``autocommit`` it runs inside ``op.get_context().autocommit_block()``
    (an enum label addition, which must be committed before a later
    statement of the revision can use the label).
    """

    def __init__(self, statement: str, kind: str, *, autocommit: bool = False) -> None:
        self.statement = statement
        self.kind = kind
        self.autocommit = autocommit

    def to_diff_tuple(self) -> tuple[Any, ...]:
        return ("ferro_execute", self.kind, self.statement)


class FerroMarkedOp(ops.MigrateOperation):
    """Ops written under one ``# ferro: <marker>`` comment line."""

    def __init__(self, marker: str, wrapped: list[ops.MigrateOperation]) -> None:
        self.marker = marker
        self.wrapped = wrapped

    def to_diff_tuple(self) -> Any:
        return [op.to_diff_tuple() for op in self.wrapped]


class FerroWarningOp(ops.MigrateOperation):
    """A report the planner made without planning an op for it (an enum
    label the model no longer declares, which ferro never removes), as a
    ``# ferro: <warning>`` comment."""

    def __init__(self, warning: str) -> None:
        self.warning = warning

    def to_diff_tuple(self) -> tuple[Any, ...]:
        return ("ferro_warning", self.warning)


class FerroRenameTableOp(ops.RenameTableOp):
    """``op.rename_table``: Alembic's own op, which its autogenerate never
    proposes and so has no renderer of its own."""

    def to_diff_tuple(self) -> tuple[Any, ...]:
        return ("rename_table", self.table_name, self.new_table_name)


class FerroIrreversibleOp(ops.MigrateOperation):
    """A step nothing undoes: ``raise RuntimeError("<reason>")``."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def to_diff_tuple(self) -> tuple[Any, ...]:
        return ("ferro_irreversible", self.reason)


class FerroRevisionOps(ops.OpContainer):
    """The whole revision ferro's comparator writes: the upgrade's ops, and
    the downgrade's (the planner run back to the live database), which
    ``reverse()`` swaps in. Never Alembic's per-op ``reverse()``."""

    def __init__(
        self,
        upgrade: list[ops.MigrateOperation],
        downgrade: list[ops.MigrateOperation],
    ) -> None:
        super().__init__(upgrade)
        self.downgrade = downgrade

    def reverse(self) -> FerroRevisionOps:
        return FerroRevisionOps(self.downgrade, list(self.ops))


def _execute_line(statement: str) -> str:
    return f"op.execute(sa.DDL({statement.replace('%', '%%')!r}))"


@renderers.dispatch_for(FerroExecuteOp)
def _render_execute(autogen_context: Any, op: FerroExecuteOp) -> list[str]:
    if not op.autocommit:
        return [_execute_line(op.statement)]
    # Alembic renders through Mako's PythonPrinter, which indents after a
    # line ending in ':' and closes the block at a blank line (#447).
    return [
        "with op.get_context().autocommit_block():",
        _execute_line(op.statement),
        "",
    ]


@renderers.dispatch_for(FerroMarkedOp)
def _render_marked(autogen_context: Any, op: FerroMarkedOp) -> list[str]:
    lines = [f"# ferro: {op.marker}"]
    for wrapped in op.wrapped:
        lines.extend(render_op(autogen_context, wrapped))
    return lines


@renderers.dispatch_for(FerroWarningOp)
def _render_warning(autogen_context: Any, op: FerroWarningOp) -> list[str]:
    return [f"# ferro: {line}" for line in op.warning.splitlines()]


@renderers.dispatch_for(FerroRenameTableOp)
def _render_rename_table(autogen_context: Any, op: FerroRenameTableOp) -> str:
    return f"op.rename_table({op.table_name!r}, {op.new_table_name!r})"


@renderers.dispatch_for(FerroIrreversibleOp)
def _render_irreversible(autogen_context: Any, op: FerroIrreversibleOp) -> list[str]:
    return [f"raise RuntimeError({op.reason!r})"]


@renderers.dispatch_for(FerroRevisionOps)
def _render_revision(autogen_context: Any, op: FerroRevisionOps) -> list[str]:
    lines: list[str] = []
    for inner in op.ops:
        lines.extend(render_op(autogen_context, inner))
    return lines


# -- the target side as SQLAlchemy ---------------------------------------------------


class _Target:
    """The schema a plan leads to, as the bridge's ``sa.Table`` objects: the
    models for ``upgrade()``, the live database for ``downgrade()``."""

    def __init__(self, envelope: dict[str, Any], dialect: str) -> None:
        from .alembic import _build_sa_table_from_ir, _naming_metadata

        self.dialect = dialect
        self.metadata = _naming_metadata()
        self.models: dict[str, dict[str, Any]] = {}
        for model in envelope["payload"]["models"]:
            self.models[model["table_name"]] = model
            _build_sa_table_from_ir(self.metadata, model)

    def table(self, name: str) -> sa.Table:
        return self.metadata.tables[name]

    def column(self, table: str, name: str) -> sa.Column:
        """A copy of the column, enum types left for the planner's own
        ``CREATE TYPE`` (Postgres)."""
        return _not_creating_types(self.table(table).columns[name], self.dialect)

    def model_column(self, table: str, name: str) -> dict[str, Any]:
        return next(c for c in self.models[table]["columns"] if c["name"] == name)

    def foreign_key(self, table: str, column: str) -> dict[str, Any] | None:
        return next(
            (
                fk
                for fk in self.models[table].get("foreign_keys") or []
                if fk["column"] == column
            ),
            None,
        )

    def has_column_check(self, table: str, column: str) -> bool:
        return any(
            check.get("column") == column
            for check in self.models[table].get("checks") or []
        )


def _not_creating_types(column: sa.Column, dialect: str) -> sa.Column:
    """``column`` (copied) whose native enum type the table op does not
    create: on Postgres every enum type a revision needs is created by the
    planner's ``CreateEnumType`` (or already exists), so SQLAlchemy must not
    issue its own ``CREATE TYPE`` beside the table (#443)."""
    copied = column._copy()
    if dialect == "postgres" and isinstance(column.type, sa.Enum) and column.type.name:
        from sqlalchemy.dialects import postgresql

        copied.type = column.type.adapt(postgresql.ENUM, create_type=False)
    return copied


def _create_table(target: _Target, table: str) -> list[ops.MigrateOperation]:
    source = target.table(table)
    columns = [_not_creating_types(column, target.dialect) for column in source.columns]
    constraints = [
        constraint._copy()
        for constraint in source.constraints
        if not isinstance(constraint, sa.PrimaryKeyConstraint)
    ]
    copy = sa.Table(
        table,
        sa.MetaData(naming_convention=target.metadata.naming_convention),
        *columns,
    )
    for constraint in constraints:
        copy.append_constraint(constraint)
    created: list[ops.MigrateOperation] = [ops.CreateTableOp.from_table(copy)]
    for index in sorted(source.indexes, key=lambda index: index.name or ""):
        created.append(
            ops.CreateIndexOp(
                index.name,
                table,
                [column.name for column in index.columns],
                unique=bool(index.unique),
            )
        )
    return created


def _sa_type(target: _Target, table: str, column: str) -> sa.types.TypeEngine:
    return _not_creating_types(target.table(table).columns[column], target.dialect).type


def _using(statements: list[str]) -> str | None:
    """The ``USING`` cast of the pass's ``ALTER COLUMN … TYPE`` statement."""
    head, sep, cast = statements[0].rpartition(" USING ")
    return cast if sep and head else None


# -- the translation --------------------------------------------------------------------


_EXECUTED = {
    "CreateEnumType",
    "DropEnumType",
    "RenameEnumLabel",
    "RenameEnumType",
    "RenameIndex",
    "RenameConstraint",
    "RenamePolicy",
    "AddCheck",
    "RebuildCheck",
    "DropCheck",
    "ValidateConstraint",
    "RebuildIndex",
    "RebuildForeignKey",
    "AddRowPolicy",
    "RebuildRowPolicy",
    "DropRowPolicy",
    "EnableRowSecurity",
    "ForceRowSecurity",
    "DisableRowSecurity",
    "NoForceRowSecurity",
    "RestoreCheck",
    "RestoreRowPolicy",
}
"""Ops with no Alembic twin: the revision runs the planner's statements."""

_SNAPSHOT_ONLY = {"RemoveEnumLabel"}
"""Ops planned only between two declared snapshots (a label removal, #536):
the bridge diffs a live database, so meeting one is a bug, refused loudly."""


def _executed(
    op: dict[str, Any], statements: list[str] | None = None
) -> list[ops.MigrateOperation]:
    return [
        FerroExecuteOp(statement, op["kind"])
        for statement in (op["statements"] if statements is None else statements)
    ]


def _foreign_key(
    target: _Target, table: str, column: str
) -> list[ops.MigrateOperation]:
    """``op.create_foreign_key`` for the declared foreign key on
    ``table.column``."""
    fk = target.foreign_key(table, column)
    if fk is None:
        raise RuntimeError(
            f"ferro: the plan adds a foreign key on {table}.{column} the models do "
            "not declare; this is a ferro bug, please file an issue"
        )
    return [
        ops.CreateForeignKeyOp(
            fk.get("name"),
            table,
            fk["to_table"],
            [column],
            [fk.get("to_column") or "id"],
            ondelete=fk.get("on_delete"),
        )
    ]


def _twin(op: dict[str, Any], target: _Target) -> list[ops.MigrateOperation]:
    """The Alembic op(s) for one planner op."""
    kind = op["kind"]
    statements: list[str] = op["statements"]
    if kind == "AddEnumLabel":
        return [FerroExecuteOp(s, kind, autocommit=True) for s in statements]
    if kind in _EXECUTED:
        return _executed(op)
    if kind == "AddTable":
        table = op["table"]
        return _create_table(target, table) + _executed(
            op, op.get("row_security_statements") or []
        )
    if kind == "DropTable":
        return [ops.DropTableOp(op["table"])]
    if kind == "RenameTable":
        return [FerroRenameTableOp(op["old"], op["new"])]
    if kind == "RenameColumn":
        return [ops.AlterColumnOp(op["table"], op["old"], modify_name=op["new"])]
    if kind == "AddColumn":
        table, column = op["table"], op["column"]
        declared = target.model_column(table, column)
        # The twin carries the column; it cannot carry the literal default the
        # pass backfills existing rows with, nor SQLite's inline REFERENCES /
        # CHECK (Alembic cannot add a constraint on SQLite): those columns run
        # as the pass writes them.
        inline_constraints = target.dialect == "sqlite" and (
            target.foreign_key(table, column) is not None
            or target.has_column_check(table, column)
        )
        if not statements:
            # No pass statement: a column that demands values of existing
            # rows, written plain (and marked) with its foreign key.
            return [ops.AddColumnOp(table, target.column(table, column))] + (
                _foreign_key(target, table, column)
                if target.foreign_key(table, column)
                else []
            )
        if declared.get("default") is not None or inline_constraints:
            return _executed(op)
        return [ops.AddColumnOp(table, target.column(table, column))] + _executed(
            op, statements[1:]
        )
    if kind == "DropColumn":
        return [ops.DropColumnOp(op["table"], op["column"])]
    if kind == "AlterColumnNullability":
        table, column = op["table"], op["column"]
        return [
            ops.AlterColumnOp(
                table,
                column,
                modify_nullable=bool(target.model_column(table, column)["nullable"]),
                existing_type=_sa_type(target, table, column),
            )
        ] + _executed(op, statements[1:])
    if kind == "AlterColumnType":
        table, column = op["table"], op["column"]
        kw: dict[str, Any] = {}
        using = _using(statements)
        if using is not None:
            kw["postgresql_using"] = using
        return [
            ops.AlterColumnOp(
                table,
                column,
                modify_type=_sa_type(target, table, column),
                existing_nullable=bool(target.model_column(table, column)["nullable"]),
                **kw,
            )
        ] + _executed(op, statements[1:])
    if kind == "AddIndex":
        return [
            ops.CreateIndexOp(
                op["name"], op["table"], list(op["columns"]), unique=bool(op["unique"])
            )
        ]
    if kind == "DropIndex":
        return [ops.DropIndexOp(op["name"], table_name=op["table"])]
    if kind == "AddForeignKey":
        return _foreign_key(target, op["table"], op["column"])
    if kind == "DropForeignKey":
        return [ops.DropConstraintOp(op["name"], op["table"], type_="foreignkey")]
    raise RuntimeError(
        f"ferro: the planner planned a {kind} op the Alembic bridge has no translation for; "
        "this is a ferro bug, please file an issue"
    )


def _marker(
    op: dict[str, Any], verdict: dict[str, Any] | None, direction: Direction
) -> str | None:
    if verdict is None:
        return None
    subject = op.get("table") or op.get("type_name") or ""
    if verdict.get("needs") == "backfill":
        return (
            f"data-dependent (fails while {subject} has rows; ferro migrations "
            f"generate the backfill: `ferro migrate new`)"
        )
    if direction == "up" and verdict.get("drops_data"):
        what = op.get("column") and f"{subject}.{op['column']}" or subject
        return f"destructive (drops {what} and the data it holds)"
    return None


def translate(
    plan: dict[str, Any], *, direction: Direction
) -> list[ops.MigrateOperation]:
    """The Alembic ops that write ``plan``, in its order.

    ``plan`` is the planner's rendered plan JSON (``_plan_from_ir(...,
    render=True)`` going up, ``_plan_reverse_from_ir`` going down) as the
    bridge hands it over: ``plan["target"]`` is the envelope the plan leads
    to (the models going up, the live database going down) and
    ``plan["dialect"]`` its dialect; each op may carry the generator's
    ``verdict`` (``_plan_step_verdicts``) for its marker. An op with
    ``irreversible`` becomes ``raise RuntimeError(<reason>)``. Going up, the
    planner's ``warnings`` (reports with no op, such as an enum label the
    model no longer declares) lead the revision as comments; its
    ``always_warnings`` (a foreign or unverifiable row policy) stay the
    connect-time warnings they are (ADR-0019).
    """
    target = _Target(plan["target"], plan["dialect"])
    out: list[ops.MigrateOperation] = []
    if direction == "up":
        out.extend(FerroWarningOp(warning) for warning in plan.get("warnings") or [])
    for op in plan["operations"]:
        if op["kind"] in _SNAPSHOT_ONLY:
            raise RuntimeError(
                f"ferro: the plan carries a {op['kind']} op, which only two declared "
                "snapshots plan (`ferro migrate new`), never the live database the "
                "Alembic bridge diffs; this is a ferro bug, please file an issue"
            )
        irreversible = op.get("irreversible")
        if irreversible is not None:
            out.append(FerroIrreversibleOp(irreversible["reason"]))
            continue
        verdict = op.get("verdict") or {}
        if not op["statements"] and verdict.get("needs") != "backfill":
            # The pass runs nothing for it on this dialect (row security of
            # a new SQLite table is its create's warning).
            continue
        written = _twin(op, target)
        marker = _marker(op, op.get("verdict"), direction)
        if marker is not None:
            out.append(FerroMarkedOp(marker, written))
        else:
            out.extend(written)
    return out
