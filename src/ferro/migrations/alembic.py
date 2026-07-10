import json
from typing import Any, Dict

try:
    import sqlalchemy as sa
except ImportError:
    sa = None

from .._annotation_utils import _VARCHAR_RE
from .._core import _ddl_fk_name, _render_check_body, _resolve_storage_type

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
