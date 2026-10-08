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

Every op comes from ``_core._plan_revision`` (``plan_revision`` in the core),
which decides what the revision holds: each op of ``upgrade()`` and
``downgrade()`` in the planner's order, its statements, whether Alembic's
own op writes it, its ``# ferro:`` marker and any irreversible reason.
This module only writes that answer, and decides nothing. Where the core
says Alembic has a twin (a table, a column, its type and nullability, an
index, a foreign key, a table or column rename) the revision uses it, built
from the same ``sa.Column`` the bridge's ``get_metadata()`` builds.
Everything else runs the statement the reconciliation pass would run, byte
for byte, as ``op.execute(sa.DDL(...))``. A marked op sits under its
``# ferro: <marker>`` comment; an irreversible one renders as
``raise RuntimeError("<reason>")``.
"""

from __future__ import annotations

from typing import Any

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
    """The schema a revision's ops lead to, as the bridge's ``sa.Table``
    objects: the models for ``upgrade()``, the live database for
    ``downgrade()``."""

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


_AUTOCOMMIT = {"AddEnumLabel"}
"""Ops whose statement runs in ``op.get_context().autocommit_block()``: a
label addition must be committed before a later statement of the revision
can use the label."""

_SNAPSHOT_ONLY = {"RemoveEnumLabel"}
"""Ops a live database never takes (a label removal, #536): going up the
planner never plans one from it, and going down one toward it is
irreversible (ADR-0050). Meeting one to write is a bug, refused loudly."""


def _executed(kind: str, statements: list[str]) -> list[ops.MigrateOperation]:
    return [
        FerroExecuteOp(statement, kind, autocommit=kind in _AUTOCOMMIT)
        for statement in statements
    ]


def _foreign_key(
    target: _Target, table: str, column: str
) -> list[ops.MigrateOperation]:
    """``op.create_foreign_key`` for the foreign key the target declares on
    ``table.column``."""
    fk = target.foreign_key(table, column)
    if fk is None:
        raise RuntimeError(
            f"ferro: the plan adds a foreign key on {table}.{column} the target does "
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


def _twin(written: dict[str, Any], target: _Target) -> list[ops.MigrateOperation]:
    """Alembic's own op(s) for one revision op the core writes as its twin,
    built from the side the revision leads to; a statement of the pass's
    beyond the one the twin stands for (an index riding an added column, a
    row-security statement after a created table) runs as written."""
    op = written["op"]
    kind = op["kind"]
    statements: list[str] = written["statements"]
    if kind == "AddTable":
        return _create_table(target, op["table"]) + _executed(
            kind, written["row_security_statements"]
        )
    if kind == "DropTable":
        return [ops.DropTableOp(op["table"])]
    if kind == "RenameTable":
        return [FerroRenameTableOp(op["old"], op["new"])]
    if kind == "RenameColumn":
        return [ops.AlterColumnOp(op["table"], op["old"], modify_name=op["new"])]
    if kind == "AddColumn":
        table, column = op["table"], op["column"]
        added: list[ops.MigrateOperation] = [
            ops.AddColumnOp(table, target.column(table, column))
        ]
        if not statements:
            # The pass has no statement for it (a column that demands values
            # of existing rows): the plain op, with its foreign key.
            if target.foreign_key(table, column) is not None:
                added += _foreign_key(target, table, column)
            return added
        return added + _executed(kind, statements[1:])
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
        ] + _executed(kind, statements[1:])
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
        ] + _executed(kind, statements[1:])
    if kind == "AddIndex":
        return [
            ops.CreateIndexOp(
                op["name"], op["table"], list(op["columns"]), unique=bool(op["unique"])
            )
        ]
    if kind == "DropIndex":
        return [ops.DropIndexOp(op["name"], table_name=op["table"])]
    if kind == "RedefineIndex":
        # Alembic's drop, then its create of the definition the revision
        # leads to (ADR-0051).
        table, name, index = op["table"], op["name"], written["index"]
        return [
            ops.DropIndexOp(name, table_name=table),
            ops.CreateIndexOp(
                name, table, list(index["columns"]), unique=bool(index["unique"])
            ),
        ]
    if kind == "AddForeignKey":
        return _foreign_key(target, op["table"], op["column"])
    if kind == "DropForeignKey":
        return [ops.DropConstraintOp(op["name"], op["table"], type_="foreignkey")]
    raise RuntimeError(
        f"ferro: the planner planned a {kind} op the Alembic bridge has no translation for; "
        "this is a ferro bug, please file an issue"
    )


def translate(
    written: list[dict[str, Any]],
    *,
    target: dict[str, Any],
    dialect: str,
    reports: list[dict[str, Any]] | None = None,
) -> list[ops.MigrateOperation]:
    """The Alembic ops that write one side of a revision, in its order.

    ``written`` is ``_core._plan_revision``'s ``upgrade`` or ``downgrade``
    list, ``target`` the envelope that side leads to (the models going up,
    the live database going down) and ``dialect`` its dialect. The core has
    decided every op: whether Alembic's own op writes it (``twin``) or
    ``op.execute`` of the pass's statements does, the ``# ferro:`` comment
    above it (``marker``) and the reason a ``raise RuntimeError(<reason>)``
    replaces it (``irreversible``). ``reports``, the revision's one-off
    reports, lead the upgrade as comments.
    """
    resolved = _Target(target, dialect)
    out: list[ops.MigrateOperation] = [
        FerroWarningOp(report["text"]) for report in reports or []
    ]
    for item in written:
        if item["irreversible"] is not None:
            out.append(FerroIrreversibleOp(item["irreversible"]))
            continue
        kind = item["op"]["kind"]
        if kind in _SNAPSHOT_ONLY:
            raise RuntimeError(
                f"ferro: the plan carries a {kind} op, which only two declared "
                "snapshots plan (`ferro migrate new`), never the live database the "
                "Alembic bridge diffs; this is a ferro bug, please file an issue"
            )
        alembic_ops = (
            _twin(item, resolved)
            if item["twin"]
            else _executed(kind, item["statements"])
        )
        marker = item["marker"]
        if marker is not None:
            out.append(FerroMarkedOp(marker["comment"], alembic_ops))
        else:
            out.extend(alembic_ops)
    return out
