import json
from typing import Any, Dict

try:
    import sqlalchemy as sa
except ImportError:
    sa = None

from .._annotation_utils import _VARCHAR_RE
from .._core import (
    _ddl_fk_name,
    _plan_check_addition,
    _plan_check_drop,
    _plan_check_rebuild,
    _plan_enum_label_addition,
    _render_check_body,
    _render_table_check_body,
    _resolve_storage_type,
)

#: SQLAlchemy ``naming_convention`` mirroring the Rust emitter's names. IR-backed
#: metadata names every artifact explicitly; this convention covers any
#: SQLAlchemy-generated fallback names during autogenerate.
_FERRO_NAMING_CONVENTION = {
    "ix": "idx_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
}


def get_metadata() -> "sa.MetaData":
    """
    Generate a SQLAlchemy MetaData object representing all registered Ferro models.
    This is intended to be used in alembic's env.py for autogenerate support.

    Enum columns are mapped to named ``sqlalchemy.Enum`` types so PostgreSQL
    autogenerate and DDL compilation succeed (anonymous enums are rejected).
    When the field annotation is a Python ``enum.Enum`` subclass, the database
    type name defaults to the enum class name in lowercase; otherwise the
    column name is used as the type name.

    For :class:`~ferro.base.ForeignKey` fields with ``unique=True`` (one-to-one
    relations), the shadow ``*_id`` column is emitted with ``Column(unique=True)``
    so Alembic autogenerate includes the matching UNIQUE constraint.

    **Column nullability:** ``Column.nullable`` follows :class:`~ferro.base.FerroField`
    / :class:`~ferro.base.ForeignKey` ``nullable`` when set to a boolean (force
    NULL / NOT NULL). The default ``nullable='infer'`` uses whether the Python
    annotation allows ``None`` (after unwrapping ``Annotated``). Shadow ``*_id``
    columns infer from the **forward relation** field's annotation, not from the
    synthetic ``*_id`` field. Primary key columns are always ``nullable=False``.
    Pydantic "required" and JSON-schema defaults do not change inferred nullability.
    """
    if sa is None:
        raise ImportError(
            "SQLAlchemy is required to use the alembic bridge. "
            "Install it via 'pip install ferro-orm[alembic]'."
        )

    metadata = sa.MetaData(naming_convention=_FERRO_NAMING_CONVENTION)

    from .. import ensure_resolved_modelset

    schema_ir = ensure_resolved_modelset()
    payload = schema_ir.get("payload", {})
    models = payload.get("models", [])
    for model_ir in models:
        if isinstance(model_ir, dict):
            _build_sa_table_from_ir(metadata, model_ir)

    return metadata


def _build_sa_table_from_ir(metadata: "sa.MetaData", model_ir: Dict[str, Any]) -> None:
    table_name = model_ir.get("table_name")
    if not isinstance(table_name, str) or not table_name:
        return

    columns = []
    columns_by_name: dict[str, Any] = {}
    for col in model_ir.get("columns") or []:
        if not isinstance(col, dict):
            continue
        col_name = col.get("name")
        if not isinstance(col_name, str) or not col_name:
            continue
        sa_type = _sa_type_from_ir_column(col_name, col)
        # No unique=/index= column flags: single-column uniques and indexes are
        # explicit named artifacts below (the IR `uniques[]`/`indexes[]` carry
        # the shared `uq_`/`idx_` names), matching the Rust emitter's shapes.
        kwargs = {
            "primary_key": bool(col.get("primary_key", False)),
            "nullable": bool(col.get("nullable", True))
            if not bool(col.get("primary_key", False))
            else False,
        }
        columns.append(sa.Column(col_name, sa_type, **kwargs))
        columns_by_name[col_name] = columns[-1]

    table_args: list[Any] = list(columns)

    for check in model_ir.get("checks") or []:
        if not isinstance(check, dict):
            continue
        name = check.get("name")
        column = check.get("column")
        values = check.get("values")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(column, str) or not column:
            continue
        if not isinstance(values, list) or not values:
            continue
        sqltext = _render_check_body(column, values)
        table_args.append(sa.CheckConstraint(sqltext, name=name))

    # Table checks (ADR-0012): the same named CHECKs the Rust emitter folds
    # into CREATE TABLE, with the body rendered by the shared Rust renderer
    # over the IR predicate — never a second body language (I-1).
    for table_check in model_ir.get("table_checks") or []:
        if not isinstance(table_check, dict):
            continue
        name = table_check.get("name")
        predicate = table_check.get("predicate")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(predicate, dict) or not predicate:
            continue
        sqltext = _render_table_check_body(json.dumps(predicate))
        table_args.append(sa.CheckConstraint(sqltext, name=name))

    # Every `uniques[]` entry — single-column included — is a standalone named
    # unique index, the one shape the Rust emitter, the SQLite ALTER path, and
    # reflection all share (FF-B B4/D1). A UniqueConstraint would reflect as a
    # different structural shape and produce phantom diffs.
    unique_index_args: list[tuple[str, list[str]]] = []
    for unique in model_ir.get("uniques") or []:
        if not isinstance(unique, dict):
            continue
        cols = unique.get("columns")
        name = unique.get("name")
        if not isinstance(cols, list) or len(cols) < 1:
            continue
        if not all(isinstance(c, str) and c for c in cols):
            continue
        if not isinstance(name, str) or not name:
            continue
        unique_index_args.append((name, cols))

    table = sa.Table(table_name, metadata, *table_args)

    for fk in model_ir.get("foreign_keys") or []:
        if not isinstance(fk, dict):
            continue
        col_name = fk.get("column")
        to_table = fk.get("to_table")
        to_column = fk.get("to_column") or "id"
        if not isinstance(col_name, str) or col_name not in columns_by_name:
            continue
        if not isinstance(to_table, str) or not to_table:
            continue
        if not isinstance(to_column, str) or not to_column:
            continue
        on_delete = fk.get("on_delete")
        fk_ir_name = fk.get("name")
        constraint_name = (
            fk_ir_name
            if isinstance(fk_ir_name, str) and fk_ir_name
            else _ddl_fk_name(table_name, col_name, to_table)
        )
        table.append_constraint(
            sa.ForeignKeyConstraint(
                [col_name],
                [f"{to_table}.{to_column}"],
                name=constraint_name,
                ondelete=on_delete if isinstance(on_delete, str) else None,
            )
        )

    for name, cols in unique_index_args:
        if not all(c in table.columns for c in cols):
            continue
        sa.Index(name, *(table.columns[c] for c in cols), unique=True)

    for index in model_ir.get("indexes") or []:
        if not isinstance(index, dict):
            continue
        cols = index.get("columns")
        name = index.get("name")
        unique = bool(index.get("unique", False))
        if not isinstance(cols, list) or not cols:
            continue
        if not isinstance(name, str) or not name:
            continue
        if not all(isinstance(c, str) and c in table.columns for c in cols):
            continue
        sa.Index(name, *(table.columns[c] for c in cols), unique=unique)


def _sa_type_from_ir_column(col_name: str, col: Dict[str, Any]) -> "sa.types.TypeEngine":
    """Mechanical consumer of the shared derived-type decision table (FF-B B2).

    The storage decision — explicit ``db_type`` wins, then enum values select
    native enum storage, then the ``(logical_type, format)`` cascade — is made
    by ``ferro_ddl_lowering::resolve_column_storage`` over FFI; this function
    only maps the resolved token/enum onto SQLAlchemy types. The dialect is
    fixed to ``"postgres"`` (the richer vocabulary): SQLAlchemy applies its own
    per-dialect lowering, which matches the Rust emitter's dialect splits
    (pinned by tests/test_db_type_cross_emitter_parity.py).
    """
    # SchemaColumn's non-Option fields must be present for deserialization;
    # defaults cover callers that pass minimal column dicts.
    column_ir = {
        "logical_type": "unknown",
        "nullable": True,
        "primary_key": False,
        "autoincrement": False,
        "unique": False,
        "index": False,
        "default": None,
        "format": None,
        "postgres_native_enum": False,
        **col,
        "name": col_name,
    }
    resolved = json.loads(_resolve_storage_type(json.dumps(column_ir), "postgres"))
    if resolved["kind"] == "pg_enum":
        return sa.Enum(*resolved["labels"], name=resolved["name"])
    mapped = _db_type_to_sa_type(resolved["token"])
    if mapped is None:
        raise RuntimeError(
            f"resolve_column_storage returned unmapped token {resolved['token']!r} "
            f"for column {col_name!r} — extend _db_type_to_sa_type (see AGENTS.md I-1)"
        )
    return mapped


#: SQLAlchemy RENDERING of the shared ``db_type`` token vocabulary. This is
#: not a second decision table: which token a column gets is decided by
#: ``ferro_ddl_lowering::resolve_column_storage``/``canonical_to_db_type_token``
#: (consumed over FFI in ``_sa_type_from_ir_column``); this function only maps
#: each token 1:1 onto an SA type. Exhaustiveness over the full vocabulary is
#: pinned by tests/test_db_type_cross_emitter_parity.py. See AGENTS.md § I-1.
def _db_type_to_sa_type(token: str) -> "sa.types.TypeEngine | None":
    """Return the SA type for a canonical ``db_type`` token, or ``None`` if
    unrecognized. Validation at class-definition time (see metaclass) means an
    unrecognized token reaching here is a programming error."""
    if sa is None:
        return None

    if token == "text":
        return sa.Text()
    if token == "smallint":
        return sa.SmallInteger()
    if token == "int":
        return sa.Integer()
    if token == "bigint":
        return sa.BigInteger()
    if token == "uuid":
        return sa.Uuid() if hasattr(sa, "Uuid") else sa.String(36)
    if token == "timestamp":
        return sa.DateTime(timezone=False)
    if token == "timestamptz":
        return sa.DateTime(timezone=True)
    if token == "date":
        return sa.Date()
    if token == "time":
        return sa.Time()
    if token == "boolean":
        return sa.Boolean()
    if token == "double":
        return sa.Double()
    if token == "numeric":
        return sa.Numeric()
    if token == "json":
        return sa.JSON()
    if token == "jsonb":
        # One SA type per token, SQLAlchemy carrying the dialect split
        # (ADR-0004): JSONB on Postgres, plain JSON on SQLite — mirroring the
        # Rust emitter's token-seam lowering. A bare postgresql.JSONB() would
        # fail to compile on SQLite.
        from sqlalchemy.dialects import postgresql

        return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    if token in {"bytea", "blob"}:
        return sa.LargeBinary()
    if token == "varchar":
        return sa.String()

    match = _VARCHAR_RE.match(token)
    if match is not None:
        return sa.String(length=int(match.group(1)))
    if token.startswith("char(") and token.endswith(")"):
        try:
            return sa.CHAR(int(token[5:-1]))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Label addition comparator (ADR-0011; CONTEXT.md *label addition*).
#
# Alembic core is blind to enum label drift: autogenerate against a database
# whose native enum type is missing a model-declared label produces an empty
# revision, and the first use of the new member fails at runtime (#328). This
# comparator is the second mechanical consumer of the label-addition decision
# table (AGENTS.md § I-1): the diff AND the rendered statements come from the
# Rust core over FFI (`_plan_enum_label_addition`), byte-identical to what the
# auto-migrate reconciliation pass executes.
#
# There is no `migrate_updates` gate here — running autogenerate is itself the
# request for a diff; parity is in the decision, not the gate. The generated
# ops render inside `op.get_context().autocommit_block()` so the revision is
# legal on every supported Postgres version and the label is committed before
# any table op that references it; label additions are inserted ahead of the
# revision's table ops for the same reason. Extra live labels render as a
# comment (warn-never-act): removal is reviewed-migration territory.
# ---------------------------------------------------------------------------

try:
    from alembic.autogenerate import comparators as _alembic_comparators
    from alembic.autogenerate import renderers as _alembic_renderers
    from alembic.operations.ops import MigrateOperation as _MigrateOperation
except ImportError:  # pragma: no cover - alembic optional at import time
    _alembic_comparators = None


if _alembic_comparators is not None:

    class AddEnumLabelsOp(_MigrateOperation):
        """Autogenerate carrier for one ferro-owned enum type's label drift.

        Renders to plain ``op.execute`` calls — a generated revision does not
        import ferro to run.
        """

        def __init__(
            self,
            type_name: str,
            statements: list[str],
            extra_labels: list[str],
        ) -> None:
            self.type_name = type_name
            self.statements = statements
            self.extra_labels = extra_labels

        def to_diff_tuple(self):
            return (
                "ferro_add_enum_labels",
                self.type_name,
                tuple(self.statements),
                tuple(self.extra_labels),
            )

        def reverse(self):
            # Labels cannot be removed in place; the downgrade is a no-op by
            # the same warn-never-act contract that governs upgrades.
            return AddEnumLabelsOp(self.type_name, [], [])

    @_alembic_comparators.dispatch_for("schema")
    def _compare_enum_labels(autogen_context, upgrade_ops, schemas) -> None:
        if autogen_context.dialect.name != "postgresql":
            return
        metadata = autogen_context.metadata
        if metadata is None:
            return

        # Declared native enum types: the named sa.Enum types the bridge maps
        # from the shared storage decision (`_sa_type_from_ir_column`).
        declared: dict[str, list[str]] = {}
        for table in metadata.tables.values():
            for column in table.columns:
                if isinstance(column.type, sa.Enum) and column.type.name:
                    declared.setdefault(str(column.type.name), list(column.type.enums))
        if not declared:
            return

        # Live labels per type in enum sort order — the same catalog read the
        # reconciliation pass takes, scoped to the connection's schema.
        rows = autogen_context.connection.execute(
            sa.text(
                "SELECT t.typname AS type_name, e.enumlabel AS label "
                "FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "JOIN pg_enum e ON e.enumtypid = t.oid "
                "WHERE n.nspname = current_schema() "
                "ORDER BY t.typname, e.enumsortorder"
            )
        ).fetchall()
        live: dict[str, list[str]] = {}
        for row in rows:
            live.setdefault(row.type_name, []).append(row.label)

        # A live type with no model-derived counterpart is user-owned; a
        # declared type absent live belongs to table-creation ops. Insert
        # drifted types ahead of the table ops, in deterministic order.
        drifted = []
        for type_name in sorted(declared):
            if type_name not in live:
                continue
            plan = json.loads(
                _plan_enum_label_addition(
                    type_name, declared[type_name], live[type_name]
                )
            )
            if plan["statements"] or plan["extra_labels"]:
                drifted.append(
                    AddEnumLabelsOp(type_name, plan["statements"], plan["extra_labels"])
                )
        upgrade_ops.ops[:0] = drifted

    # -----------------------------------------------------------------------
    # Check-addition / check-rebuild / leftover-drop comparator (ADR-0013,
    # ADR-0015; CONTEXT.md *table check*, *column check*, *constraint rebuild*).
    #
    # Alembic core does not compare CHECK constraints, so autogenerate against
    # a database whose table is missing a declared ``ck_*`` — or whose live
    # body drifted, or whose live ferro-owned ``ck_*`` the model no longer
    # declares — produces an empty revision and the invariant silently goes
    # unenforced. This comparator is the mechanical consumer of all three
    # decision tables (AGENTS.md § I-1): the diff AND the rendered statements
    # come from the Rust core over FFI (``_plan_check_addition``,
    # ``_plan_check_rebuild``, ``_plan_check_drop``), byte-identical to what
    # the auto-migrate reconciliation pass executes. There is no Python
    # normalizer.
    #
    # There is no ``migrate_updates`` / ``migrate_destructive`` gate here —
    # running autogenerate is itself the request for a diff. The destructive
    # flag is connect-time safety only (ADR-0013 / ADR-0011: parity is the
    # SQL, not the flag). Additions then rebuilds then leftover drops are
    # appended after the revision's table ops so a CHECK over a newly added
    # column lands after its ADD COLUMN. Postgres-only, like the
    # reconciliation pass: on SQLite, adding, rebuilding, or dropping a table
    # constraint needs a full table rebuild, which is Alembic's batch-mode
    # door (ADR-0014).
    # -----------------------------------------------------------------------

    class FerroCheckConstraintsOp(_MigrateOperation):
        """Autogenerate carrier for one table's missing CHECK constraints.

        Renders to plain ``op.execute`` / ``op.drop_constraint`` calls — a
        generated revision does not import ferro to run.
        """

        def __init__(
            self,
            table_name: str,
            statements: list[str],
            names: list[str],
            direction: str = "add",
        ) -> None:
            self.table_name = table_name
            self.statements = statements
            self.names = names
            self.direction = direction

        def to_diff_tuple(self):
            return (
                f"ferro_{self.direction}_check_constraints",
                self.table_name,
                tuple(self.names),
            )

        def reverse(self) -> "FerroCheckConstraintsOp":
            return FerroCheckConstraintsOp(
                self.table_name,
                self.statements,
                self.names,
                "drop" if self.direction == "add" else "add",
            )

    def _live_check_names_by_table(connection) -> dict[str, list[tuple[str, str]]]:
        """Live CHECK names + catalog definitions per ordinary table.

        A table with no CHECKs maps to an empty list — the distinction that
        keeps a table absent from the database (whose CHECKs ride ``create_table``)
        out of the comparator. Definitions are ``pg_get_constraintdef`` text;
        the Rust normalizer (ADR-0015) decides whether a same-name body drifted.
        """
        rows = connection.execute(
            sa.text(
                "SELECT rel.relname AS table_name, con.conname AS name, "
                "       pg_get_constraintdef(con.oid)::text AS definition "
                "FROM pg_class rel "
                "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
                "LEFT JOIN pg_constraint con "
                "  ON con.conrelid = rel.oid AND con.contype = 'c' "
                "WHERE nsp.nspname = current_schema() AND rel.relkind = 'r' "
                "ORDER BY rel.relname, con.conname"
            )
        ).fetchall()
        live: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            checks = live.setdefault(row.table_name, [])
            if row.name is not None:
                checks.append((row.name, row.definition or ""))
        return live

    @_alembic_comparators.dispatch_for("schema")
    def _compare_check_constraints(autogen_context, upgrade_ops, schemas) -> None:
        if autogen_context.dialect.name != "postgresql":
            return
        metadata = autogen_context.metadata
        if metadata is None:
            return

        from .. import ensure_resolved_modelset

        models = ensure_resolved_modelset().get("payload", {}).get("models", [])
        if not models:
            return

        live = _live_check_names_by_table(autogen_context.connection)

        # The declared side is the compiled SchemaIR — the structured predicate
        # the shared renderer needs, which the reflected SA metadata does not
        # carry. Tables absent live belong to the revision's create_table ops
        # (which already render their CHECKs inline).
        additions = []
        rebuilds = []
        drops = []
        for model_ir in models:
            if not isinstance(model_ir, dict):
                continue
            table_name = model_ir.get("table_name")
            if not isinstance(table_name, str) or table_name not in live:
                continue
            if table_name not in metadata.tables:
                continue
            live_checks = live[table_name]
            live_names = [name for name, _ in live_checks]
            add_plan = json.loads(
                _plan_check_addition(table_name, json.dumps(model_ir), live_names)
            )
            if add_plan["statements"]:
                additions.append(
                    FerroCheckConstraintsOp(
                        table_name, add_plan["statements"], add_plan["names"]
                    )
                )
            rebuild_plan = json.loads(
                _plan_check_rebuild(table_name, json.dumps(model_ir), live_checks)
            )
            if rebuild_plan["statements"]:
                rebuilds.append(
                    FerroCheckRebuildOp(
                        table_name, rebuild_plan["statements"], rebuild_plan["names"]
                    )
                )
            live_ferro_owned = [
                name for name, _ in live_checks if name.startswith("ck_")
            ]
            drop_plan = json.loads(
                _plan_check_drop(table_name, json.dumps(model_ir), live_ferro_owned)
            )
            if drop_plan["statements"]:
                drops.append(
                    FerroCheckDropOp(
                        table_name, drop_plan["statements"], drop_plan["names"]
                    )
                )
        upgrade_ops.ops.extend(additions)
        upgrade_ops.ops.extend(rebuilds)
        upgrade_ops.ops.extend(drops)

    class FerroCheckRebuildOp(_MigrateOperation):
        """Autogenerate carrier for one table's drifted CHECK bodies.

        Renders to plain ``op.execute`` of the Rust-rendered DROP + bare ADD
        (I-1) — a generated revision does not import ferro to run. There is no
        ``migrate_updates`` gate: running autogenerate is itself the request
        for a diff.
        """

        def __init__(
            self,
            table_name: str,
            statements: list[str],
            names: list[str],
        ) -> None:
            self.table_name = table_name
            self.statements = statements
            self.names = names

        def to_diff_tuple(self):
            return (
                "ferro_rebuild_check_constraints",
                self.table_name,
                tuple(self.names),
            )

        def reverse(self) -> "FerroCheckRebuildOp":
            # The previous body is not in the generated revision; restoring it
            # is a reviewed edit, not an autogenerated downgrade.
            return FerroCheckRebuildOp(self.table_name, [], self.names)

    class FerroCheckDropOp(_MigrateOperation):
        """Autogenerate carrier for one table's leftover ferro-owned CHECKs.

        Renders to plain ``op.execute`` of the Rust-rendered DROP (I-1) — a
        generated revision does not import ferro to run. There is no
        ``migrate_destructive`` gate: running autogenerate is itself the
        request for a diff.
        """

        def __init__(
            self,
            table_name: str,
            statements: list[str],
            names: list[str],
        ) -> None:
            self.table_name = table_name
            self.statements = statements
            self.names = names

        def to_diff_tuple(self):
            return (
                "ferro_drop_check_constraints",
                self.table_name,
                tuple(self.names),
            )

        def reverse(self) -> "FerroCheckDropOp":
            # Recreating the leftover body is a reviewed edit, not an
            # autogenerated downgrade.
            return FerroCheckDropOp(self.table_name, [], self.names)

    @_alembic_renderers.dispatch_for(FerroCheckConstraintsOp)
    def _render_check_constraints(
        autogen_context, op: FerroCheckConstraintsOp
    ) -> list[str]:
        if op.direction == "drop":
            return [
                f"op.drop_constraint({name!r}, {op.table_name!r}, type_='check')"
                for name in reversed(op.names)
            ]
        return [f"op.execute({stmt!r})" for stmt in op.statements]

    @_alembic_renderers.dispatch_for(FerroCheckRebuildOp)
    def _render_check_rebuilds(autogen_context, op: FerroCheckRebuildOp) -> list[str]:
        return [f"op.execute({stmt!r})" for stmt in op.statements]

    @_alembic_renderers.dispatch_for(FerroCheckDropOp)
    def _render_check_drops(autogen_context, op: FerroCheckDropOp) -> list[str]:
        return [f"op.execute({stmt!r})" for stmt in op.statements]

    @_alembic_renderers.dispatch_for(AddEnumLabelsOp)
    def _render_add_enum_labels(autogen_context, op: AddEnumLabelsOp) -> list[str]:
        lines: list[str] = []
        if op.extra_labels:
            listed = ", ".join(f"'{label}'" for label in op.extra_labels)
            lines.append(
                f"# ferro: enum type '{op.type_name}' has live label(s) {listed} "
                "that the model no longer declares. Label addition is append-only "
                "and never removes labels (rows may still hold them); remove or "
                "rename them in a reviewed migration."
            )
        if op.statements:
            # Outside the migration transaction: ALTER TYPE ... ADD VALUE is
            # non-transactional before PG12, and the label must be committed
            # before any table op below can reference it.
            lines.append("with op.get_context().autocommit_block():")
            lines.extend(f"    op.execute({stmt!r})" for stmt in op.statements)
        return lines
