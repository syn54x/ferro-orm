import json
from typing import Any, Dict

try:
    import sqlalchemy as sa
except ImportError:
    sa = None

from .._annotation_utils import _VARCHAR_RE
from .._core import (
    _ddl_fk_name,
    _is_ferro_row_policy_name,
    _plan_check_addition,
    _plan_check_drop,
    _plan_check_rebuild,
    _plan_enum_label_addition,
    _plan_row_security,
    _plan_row_security_reconcile,
    _render_check_body,
    _render_table_check_body,
    _resolve_storage_type,
    _row_policy_command_from_catalog_code,
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
    from alembic.util import DispatchPriority as _AlembicDispatchPriority
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

    # -----------------------------------------------------------------------
    # Row-security comparator (PRD #406, #413/#414; AGENTS.md § I-1 entries
    # 15/16; ADR-0019).
    #
    # SQLAlchemy metadata has no policy construct, so unlike CHECK constraints
    # (which ride the native ``create_table``/``add_constraint`` diff) row
    # security needs its own comparator for BOTH a brand-new table (the
    # create-pass decision, ``_plan_row_security``) and a live one (the
    # reconciliation decision, ``_plan_row_security_reconcile``). Neither side
    # is re-derived here — the diff AND the rendered statements come from the
    # Rust core over FFI, byte-identical to what `auto_migrate` executes.
    #
    # `_plan_row_security_reconcile` is called TWICE per live table, with
    # `destructive` false then true. Its own documented statement order —
    # flags, missing creates, drifted rebuilds, then (destructive only) orphan
    # drops and flag teardown — makes the non-destructive call's statements an
    # exact prefix of the destructive call's, so slicing the tail off isolates
    # the orphan/teardown-only statements without re-deriving the diff or
    # rendering anything a second way.
    #
    # There is no `migrate_updates`/`migrate_destructive` gate on either op:
    # running autogenerate is itself the request for a diff, and orphan/
    # teardown drops are always proposed — the same posture `_plan_check_drop`
    # takes (ADR-0013): the auto-migrate flags are connect-time safety, a
    # generated revision is reviewed before it ever runs.
    #
    # Two categories the reconcile plan reports are never turned into ops, on
    # purpose, and silently: a **foreign** policy (ferro does not own it) is
    # untouched on every migration door already — the checks family's own
    # comparator has no comment precedent for "found nothing to act on", and
    # inventing one here would make every revision comment on policies nobody
    # asked ferro about. An **unverifiable** raw policy is ADR-0019's other
    # one-way case: ferro cannot tell an edited raw `using=`/`with_check=`
    # from Postgres's own re-spelling of an unedited one, so — matching the
    # runtime pass, which only *warns* about it on `migrate_updates` and never
    # touches it — autogenerate stays silent rather than emit a comment op the
    # checks family has no shape for either. Both are visible today via the
    # runtime's own connect-time warnings; a raw policy that needs a change
    # goes through a reviewed migration written by hand, or gets rewritten in
    # the shorthand form ferro can compare.
    # -----------------------------------------------------------------------

    def _live_row_security_by_table(connection) -> dict[str, Dict[str, Any]]:
        """Every ordinary table's live row-security state: the two
        ``pg_class`` flags plus every policy on it, ferro-owned or not.

        Mirrors ``src/introspect.rs``'s ``live_table_row_security`` query —
        same joins, same columns — batched across every table in one pass
        (the ``_live_check_names_by_table`` shape) instead of one table at a
        time. The command decode and the ownership test are NOT
        re-implemented here: both come from the Rust core over FFI
        (``_row_policy_command_from_catalog_code``,
        ``_is_ferro_row_policy_name``), the same functions
        ``live_table_row_security`` calls, so the two introspection paths
        cannot drift apart (AGENTS.md § I-1).

        Both queries filter ``rel.relkind = 'r'`` (ordinary tables) so a
        row this batch's two queries disagree about (a partitioned table's
        policies, say, with no matching flags row) can never fabricate a
        flags-off default for something the flags query never saw —
        ``live_table_row_security`` has no equivalent filter, but it reads
        one already-named table by ``relname`` at a time, where that
        ambiguity cannot arise.
        """
        flag_rows = connection.execute(
            sa.text(
                "SELECT rel.relname AS table_name, "
                "       rel.relrowsecurity AS enabled, "
                "       rel.relforcerowsecurity AS forced "
                "FROM pg_class rel "
                "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
                "WHERE nsp.nspname = current_schema() AND rel.relkind = 'r'"
            )
        ).fetchall()
        live: dict[str, Dict[str, Any]] = {
            row.table_name: {
                "enabled": bool(row.enabled),
                "forced": bool(row.forced),
                "policies": [],
            }
            for row in flag_rows
        }

        policy_rows = connection.execute(
            sa.text(
                "SELECT rel.relname AS table_name, "
                "       pol.polname::text AS name, "
                "       pol.polcmd::text AS cmd, "
                "       pol.polpermissive AS permissive, "
                "       pg_get_expr(pol.polqual, pol.polrelid)::text AS using_expr, "
                "       pg_get_expr(pol.polwithcheck, pol.polrelid)::text AS with_check_expr, "
                "       COALESCE(("
                "           SELECT string_agg(role_name, ',' ORDER BY role_name)"
                "           FROM ("
                "               SELECT CASE WHEN oid = 0 THEN 'public'"
                "                           ELSE pg_get_userbyid(oid)::text END AS role_name"
                "               FROM unnest(pol.polroles) AS oid"
                "           ) AS resolved"
                "       ), '') AS roles "
                "FROM pg_policy pol "
                "JOIN pg_class rel ON rel.oid = pol.polrelid "
                "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
                "WHERE nsp.nspname = current_schema() AND rel.relkind = 'r' "
                "ORDER BY rel.relname, pol.polname"
            )
        ).fetchall()
        for row in policy_rows:
            table = live.setdefault(
                row.table_name, {"enabled": False, "forced": False, "policies": []}
            )
            name = row.name
            command = _row_policy_command_from_catalog_code(row.cmd) or row.cmd
            roles = [role for role in (row.roles or "").split(",") if role]
            table["policies"].append(
                {
                    "name": name,
                    "command": command,
                    "restrictive": not bool(row.permissive),
                    "using": row.using_expr,
                    "with_check": row.with_check_expr,
                    "roles": roles,
                    "ferro_owned": _is_ferro_row_policy_name(name),
                }
            )
        return live

    def _synthetic_ferro_owned_live(
        names: list[str], *, enabled: bool, forced: bool
    ) -> Dict[str, Any]:
        """The ``LiveRowSecurity`` shape for exactly the policies ONE op is
        about to create, as they will exist immediately after its upgrade
        runs — enough for ``_teardown_row_security_statements`` to compute a
        real reverse for it.

        Command, body, and roles are irrelevant to that computation (dropping
        a policy by name and reading the flags do not need them) and are left
        at defaults; only the name and ``ferro_owned=True`` matter.

        ``enabled``/``forced`` must be the flags AS THEY WILL STAND right
        after this op's upgrade runs — never hardcoded. A table whose row
        security predates the declaration (a DBA enabled it; ferro's op only
        adds a policy) must reverse to "policy dropped, flags untouched": if
        the caller passed ``enabled=True`` unconditionally here, the computed
        reverse would ``DISABLE ROW LEVEL SECURITY`` on a fence ferro never
        turned on — the exact incident
        ``ferro_ddl_lowering::excess_row_security_flag_statements``'s own
        ownership gate exists to prevent, reached through the back door of a
        downgrade instead of ``migrate_destructive``.
        """
        return {
            "enabled": enabled,
            "forced": forced,
            "policies": [
                {
                    "name": name,
                    "command": "all",
                    "restrictive": False,
                    "using": None,
                    "with_check": None,
                    "roles": [],
                    "ferro_owned": True,
                }
                for name in names
            ],
        }

    def _teardown_row_security_statements(
        model_ir: Dict[str, Any], live_row_security: Dict[str, Any]
    ) -> list[str]:
        """What it takes to undo exactly the row security captured in
        ``live_row_security`` for this table.

        Computed by asking the SAME reconcile decision forward, with a
        declaration that no longer exists: treating the table as though its
        model declares no ``__ferro_rls__`` turns every ferro-owned policy in
        ``live_row_security`` into an orphan, and — because at least one is
        ferro-owned (`ferro_manages_row_security`) — its flags into a
        teardown too. That is exactly ADR-0013's destructive ladder and
        ADR-0019's ownership gate, already exercised by
        ``test_row_security_orphans.py``'s "declaration removed" case; this
        helper reuses it rather than rendering a second `DROP POLICY` /
        `DISABLE ROW LEVEL SECURITY` template in Python. It is how
        ``FerroRowSecurityOp.reverse()`` gets real SQL instead of an empty
        downgrade.
        """
        synthetic_model_ir = dict(model_ir)
        synthetic_model_ir["row_security"] = None
        plan = json.loads(
            _plan_row_security_reconcile(
                json.dumps(synthetic_model_ir),
                json.dumps(live_row_security),
                "postgres",
                True,
            )
        )
        return plan["statements"]

    def _teardown_flag_labels(statements: list[str]) -> list[str]:
        """Human labels for the flag-only statements inside a teardown tail
        (``"force"`` for ``NO FORCE ROW LEVEL SECURITY``, ``"enabled"`` for
        ``DISABLE ROW LEVEL SECURITY``), for ``FerroRowSecurityDropOp``'s diff
        tuple — the same text classification
        ``row_security_teardown_warning`` uses on its own rendered output, so
        a force=False flip with no orphaned policy still names *something* in
        the diff instead of an empty tuple next to real DDL.
        """
        labels = []
        for statement in statements:
            if "NO FORCE" in statement:
                labels.append("force")
            elif "DISABLE" in statement:
                labels.append("enabled")
        return labels

    class FerroRowSecurityOp(_MigrateOperation):
        """Autogenerate carrier for one table's missing row-security flags and
        policies, plus any policy rebuilt because its metadata (command,
        permissive/restrictive, clauses, or roles) drifted.

        Renders to plain ``op.execute`` calls — a generated revision does not
        import ferro to run.
        """

        def __init__(
            self,
            table_name: str,
            statements: list[str],
            names: list[str],
            reverse_statements: list[str],
        ) -> None:
            self.table_name = table_name
            self.statements = statements
            self.names = names
            self.reverse_statements = reverse_statements

        def to_diff_tuple(self):
            return ("ferro_row_security", self.table_name, tuple(self.names))

        def reverse(self) -> "FerroRowSecurityOp":
            return FerroRowSecurityOp(
                self.table_name,
                self.reverse_statements,
                self.names,
                self.statements,
            )

    class FerroRowSecurityDropOp(_MigrateOperation):
        """Autogenerate carrier for one table's leftover ferro-owned row
        policies (orphans), the flag teardown that goes with a fully removed
        declaration, AND the narrower ``force=False`` flip on its own (a
        live-declared model whose declaration still exists but no longer
        asks for ``force=True`` — ``NO FORCE ROW LEVEL SECURITY`` with no
        policy drop alongside it, so ``names`` can be flag-only labels from
        ``_teardown_flag_labels`` with no policy name in it at all).

        Renders to plain ``op.execute`` calls — a generated revision does not
        import ferro to run.
        """

        def __init__(
            self, table_name: str, statements: list[str], names: list[str]
        ) -> None:
            self.table_name = table_name
            self.statements = statements
            self.names = names

        def to_diff_tuple(self):
            return ("ferro_drop_row_security", self.table_name, tuple(self.names))

        def reverse(self) -> "FerroRowSecurityDropOp":
            # Recreating a dropped/orphaned policy's exact live body is a
            # reviewed edit, not an autogenerated downgrade — the same call
            # FerroCheckDropOp makes for a leftover CHECK, and ADR-0019's own
            # posture: ferro reports the bodies it does not own or no longer
            # declares, it does not reconstruct them. This is deliberately
            # true of the force=False flip too: an empty reverse never
            # restores FORCE ROW LEVEL SECURITY, the same "reviewed edit, not
            # an autogenerated one" call, for the same reason — the op has no
            # record of whether the live FORCE predated ferro's own
            # declaration or was ferro's to begin with.
            return FerroRowSecurityDropOp(self.table_name, [], self.names)

    # `priority=LAST`: unlike the check/enum comparators above (which only
    # ever touch a table already live — a table this SAME revision creates
    # rides its own `create_table` op's inline constraints, no separate op
    # needed), this comparator's new-table branch appends a real op that
    # must run AFTER that table exists. Alembic's own table-creation
    # comparator is merged into `autogen_context.comparators` per-instance
    # (`Plugin.populate_autogenerate_priority_dispatch`), AFTER whatever a
    # module already registered directly on the global dispatcher at import
    # time — so a same-priority (`MEDIUM`, the default) registration here
    # would run and append to `upgrade_ops.ops` BEFORE `create_table` is
    # even in the list. Running last guarantees it is there to append after.
    @_alembic_comparators.dispatch_for("schema", priority=_AlembicDispatchPriority.LAST)
    def _compare_row_security(autogen_context, upgrade_ops, schemas) -> None:
        if autogen_context.dialect.name != "postgresql":
            return
        metadata = autogen_context.metadata
        if metadata is None:
            return

        from .. import ensure_resolved_modelset

        models = ensure_resolved_modelset().get("payload", {}).get("models", [])
        if not models:
            return

        live = _live_row_security_by_table(autogen_context.connection)

        add_ops: list[FerroRowSecurityOp] = []
        drop_ops: list[FerroRowSecurityDropOp] = []
        for model_ir in models:
            if not isinstance(model_ir, dict):
                continue
            table_name = model_ir.get("table_name")
            if not isinstance(table_name, str) or not table_name:
                continue
            if table_name not in metadata.tables:
                continue

            table_live = live.get(table_name)
            if table_live is None:
                # A table THIS revision is about to create: no SA construct
                # renders row security inline (metadata has no policy
                # concept), so a declared table gets its own op here, off the
                # create-pass decision (#418) instead of the reconciliation
                # one — there is no live state to reconcile against yet.
                row_security_ir = model_ir.get("row_security")
                if not row_security_ir:
                    continue
                create_plan = json.loads(
                    _plan_row_security(json.dumps(model_ir), "postgres")
                )
                if not create_plan["statements"]:
                    continue
                # A brand-new table's create pass genuinely enables it (there
                # is no live state to have enabled it already) and forces it
                # iff the declaration asks for that — both real post-upgrade
                # facts, not an assumption.
                after_live = _synthetic_ferro_owned_live(
                    create_plan["names"],
                    enabled=True,
                    forced=bool(row_security_ir.get("force", False)),
                )
                reverse_statements = _teardown_row_security_statements(
                    model_ir, after_live
                )
                add_ops.append(
                    FerroRowSecurityOp(
                        table_name,
                        create_plan["statements"],
                        create_plan["names"],
                        reverse_statements,
                    )
                )
                continue

            model_ir_json = json.dumps(model_ir)
            live_json = json.dumps(table_live)
            add_plan = json.loads(
                _plan_row_security_reconcile(
                    model_ir_json, live_json, "postgres", False
                )
            )
            full_plan = json.loads(
                _plan_row_security_reconcile(model_ir_json, live_json, "postgres", True)
            )
            # Invariant `plan_row_security_reconcile` documents on itself:
            # the destructive call's statements begin with EXACTLY the
            # non-destructive call's statements. This comparator relies on
            # that prefix to split an add-op from a drop-op by slicing
            # rather than a second traversal — if it ever stopped holding,
            # slicing would silently misclassify teardown DDL as part of the
            # (ungated) add op. Fail loudly instead (AGENTS.md § I-6).
            add_statements = add_plan["statements"]
            prefix_len = len(add_statements)
            if full_plan["statements"][:prefix_len] != add_statements:
                raise RuntimeError(
                    f"ferro internal invariant violated for table {table_name!r}: "
                    "_plan_row_security_reconcile's destructive-call statements "
                    "must extend its non-destructive-call statements as a strict "
                    "prefix. This is a ferro bug in ferro_ddl_lowering, not a "
                    "data problem — please file an issue."
                )
            teardown_statements = full_plan["statements"][prefix_len:]

            if add_statements:
                reverse_statements: list[str] = []
                if add_plan["missing"]:
                    # A rebuilt policy's reverse is empty (mirrors
                    # FerroCheckRebuildOp): its OLD body was either
                    # unverifiable raw SQL ferro never rendered, or the
                    # server's own re-spelling of what ferro already writes,
                    # and reconstructing either one is a reviewed edit, not
                    # an autogenerated downgrade. A newly created policy has
                    # no such history, so only `missing` gets a real reverse.
                    #
                    # The post-upgrade flags fed into the synthetic live
                    # state are exactly what THIS op turned on — never a
                    # blanket `True`: a table whose row security predates
                    # the declaration (flags already on, force=False so no
                    # FORCE emitted either) must reverse to "policy dropped,
                    # flags untouched", not to `DISABLE ROW LEVEL SECURITY`
                    # on a fence ferro never enabled.
                    row_security_ir = model_ir.get("row_security") or {}
                    enable_emitted = not table_live["enabled"]
                    force_emitted = (
                        bool(row_security_ir.get("force", False))
                        and not table_live["forced"]
                    )
                    after_live = _synthetic_ferro_owned_live(
                        add_plan["missing"],
                        enabled=enable_emitted,
                        forced=force_emitted,
                    )
                    reverse_statements = _teardown_row_security_statements(
                        model_ir, after_live
                    )
                add_ops.append(
                    FerroRowSecurityOp(
                        table_name,
                        add_statements,
                        add_plan["missing"] + add_plan["drifted"],
                        reverse_statements,
                    )
                )
            if teardown_statements:
                drop_ops.append(
                    FerroRowSecurityDropOp(
                        table_name,
                        teardown_statements,
                        full_plan["extra"] + _teardown_flag_labels(teardown_statements),
                    )
                )

        upgrade_ops.ops.extend(add_ops)
        upgrade_ops.ops.extend(drop_ops)

    @_alembic_renderers.dispatch_for(FerroRowSecurityOp)
    def _render_row_security(autogen_context, op: FerroRowSecurityOp) -> list[str]:
        return [f"op.execute({stmt!r})" for stmt in op.statements]

    @_alembic_renderers.dispatch_for(FerroRowSecurityDropOp)
    def _render_row_security_drops(
        autogen_context, op: FerroRowSecurityDropOp
    ) -> list[str]:
        return [f"op.execute({stmt!r})" for stmt in op.statements]
