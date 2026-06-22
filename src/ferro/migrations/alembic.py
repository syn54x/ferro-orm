import enum
import types
import warnings
from typing import Annotated, Any, Dict, Union, get_args, get_origin

try:
    import sqlalchemy as sa
except ImportError:
    sa = None

from .._annotation_utils import _VARCHAR_RE
from ..ir import compile_registry_schema_ir
from ..schema_metadata import build_model_schema
from ..state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY

#: SQLAlchemy ``naming_convention`` keeping Alembic autogen output identical to
#: the Rust runtime DDL emitter (``src/schema.rs``). Single-column indexes use
#: ``idx_<table>_<col>`` in both paths, matching the existing
#: ``ferro_composite_indexes`` convention. See the cross-emitter DDL parity
#: invariant in ``AGENTS.md``.
_FERRO_NAMING_CONVENTION = {
    "ix": "idx_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
}


def _ck_constraint_name(table_name: str, col_name: str) -> str:
    """Canonical ``ck_<table>_<col>`` name with the 63-char Postgres guard."""
    name = f"ck_{table_name}_{col_name}"
    if len(name) > 63:
        name = name[:60] + "_ck"
    return name


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

    # 1. First, ensure all relationships are resolved
    from ..relations import resolve_relationships

    resolve_relationships()

    # 2. Build SQLAlchemy metadata from SchemaIR modelset only.
    schema_ir = compile_registry_schema_ir()
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
        kwargs = {
            "primary_key": bool(col.get("primary_key", False)),
            "nullable": bool(col.get("nullable", True))
            if not bool(col.get("primary_key", False))
            else False,
            "unique": bool(col.get("unique", False)),
            "index": bool(col.get("index", False)),
        }
        columns.append(sa.Column(col_name, sa_type, **kwargs))
        columns_by_name[col_name] = columns[-1]

    table_args: list[Any] = list(columns)

    for check in model_ir.get("checks") or []:
        if not isinstance(check, dict):
            continue
        expression = check.get("expression")
        name = check.get("name")
        if not isinstance(expression, str) or not expression:
            continue
        if not isinstance(name, str) or not name:
            continue
        table_args.append(sa.CheckConstraint(expression, name=name))

    for unique in model_ir.get("uniques") or []:
        if not isinstance(unique, dict):
            continue
        cols = unique.get("columns")
        name = unique.get("name")
        if not isinstance(cols, list) or len(cols) < 1:
            continue
        if not all(isinstance(c, str) and c for c in cols):
            continue
        if len(cols) == 1:
            column_name = cols[0]
            if bool(model_ir_column_flag(model_ir, column_name, "unique")):
                continue
        if isinstance(name, str) and name:
            table_args.append(sa.UniqueConstraint(*cols, name=name))
        else:
            table_args.append(sa.UniqueConstraint(*cols))

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
        column = table.columns[col_name]
        column.append_foreign_key(
            sa.ForeignKey(
                f"{to_table}.{to_column}",
                ondelete=on_delete if isinstance(on_delete, str) else None,
            )
        )

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
        if len(cols) == 1:
            column_name = cols[0]
            if bool(model_ir_column_flag(model_ir, column_name, "index")):
                continue
        sa.Index(name, *(table.columns[c] for c in cols), unique=unique)


def model_ir_column_flag(model_ir: Dict[str, Any], column_name: str, flag: str) -> bool:
    for col in model_ir.get("columns") or []:
        if not isinstance(col, dict):
            continue
        if col.get("name") != column_name:
            continue
        return bool(col.get(flag, False))
    return False


def _sa_type_from_ir_column(col_name: str, col: Dict[str, Any]) -> "sa.types.TypeEngine":
    db_type_explicit = bool(col.get("db_type_explicit", False))
    db_type = col.get("db_type")
    if db_type_explicit and isinstance(db_type, str):
        mapped = _db_type_to_sa_type(db_type)
        if mapped is not None:
            return mapped

    enum_values = col.get("enum_values")
    enum_type_name = col.get("enum_type_name")
    if isinstance(enum_values, list) and enum_values:
        labels = [str(v) for v in enum_values]
        enum_name = (
            enum_type_name
            if isinstance(enum_type_name, str) and enum_type_name
            else col_name
        )
        return sa.Enum(*labels, name=enum_name)

    logical_type = col.get("logical_type")
    if logical_type == "boolean":
        return sa.Boolean()
    if logical_type == "integer":
        return sa.Integer()
    if logical_type == "number":
        return sa.Float()
    if logical_type == "decimal":
        return sa.Numeric()
    if logical_type == "string":
        return sa.String()
    if logical_type in {"datetime", "date", "time", "uuid"}:
        if logical_type == "datetime":
            return sa.DateTime()
        if logical_type == "date":
            return sa.Date()
        if logical_type == "time":
            return sa.Time()
        return sa.Uuid() if hasattr(sa, "Uuid") else sa.String(36)

    if isinstance(db_type, str):
        mapped = _db_type_to_sa_type(db_type)
        if mapped is not None:
            return mapped

    return sa.String()


def _resolve_ref(schema: Dict[str, Any], col_info: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve $ref in JSON schema if present."""
    if "$ref" in col_info:
        ref_path = col_info["$ref"]
        if ref_path.startswith("#/$defs/"):
            def_name = ref_path.split("/")[-1]
            resolved = schema.get("$defs", {}).get(def_name, col_info)
            if resolved is col_info:
                return col_info
            return {
                **resolved,
                **{k: v for k, v in col_info.items() if k != "$ref"},
            }
    return col_info


def _strip_optional_union(annotation: Any) -> Any:
    """Unwrap ``T | None`` / ``Optional[T]`` to ``T``."""
    hint = annotation
    while True:
        origin = get_origin(hint)
        if origin is Union or origin is types.UnionType:
            args = [a for a in get_args(hint) if a is not type(None)]
            if len(args) == 1:
                hint = args[0]
                continue
        return hint


def _annotation_as_enum_subclass(annotation: Any) -> type[enum.Enum] | None:
    """If ``annotation`` denotes a Python ``Enum`` type, return that class."""
    hint = _strip_optional_union(annotation)
    origin = get_origin(hint)
    if origin is Annotated:
        args = get_args(hint)
        if args:
            return _annotation_as_enum_subclass(args[0])
        return None
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return hint
    return None


def _field_python_enum(
    model_cls: type[Any] | None, field_name: str
) -> type[enum.Enum] | None:
    """Return the ``Enum`` class for a model field, if any."""
    if model_cls is None:
        return None
    model_fields = getattr(model_cls, "model_fields", None)
    if not model_fields:
        return None
    field = model_fields.get(field_name)
    if field is None:
        return None
    return _annotation_as_enum_subclass(field.annotation)


def _infer_nullable_join_table(
    col_name: str,
    col_info: Dict[str, Any],
    required_fields: list[str],
) -> bool:
    """Join-table schemas without a model class: JSON-schema-only nullability."""
    if "anyOf" in col_info:
        return any(item.get("type") == "null" for item in col_info["anyOf"])
    if col_info.get("type") == "null":
        return True
    return col_name not in required_fields


def _resolve_sa_column_nullable(
    col_name: str, col_info: Dict[str, Any], required_fields: list[str]
) -> bool:
    """SQLAlchemy ``Column.nullable`` for one table column."""
    if col_info.get("primary_key"):
        return False

    override = col_info.get("ferro_nullable")
    if isinstance(override, bool):
        return override

    return _infer_nullable_join_table(col_name, col_info, required_fields)


def _build_sa_table(
    metadata: "sa.MetaData",
    table_name: str,
    schema: Dict[str, Any],
    model_cls: type[Any] | None = None,
):
    """Build a SQLAlchemy Table object from a Ferro JSON schema."""
    warnings.warn(
        "_build_sa_table() is deprecated. Alembic metadata now derives from SchemaIR. "
        "Use get_metadata() / IR-backed helpers instead. Planned removal: v0.13.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    columns = []

    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])

    db_check_columns: list[tuple[str, type[enum.Enum] | None, list[Any]]] = []

    for col_name, col_info in properties.items():
        # Resolve $ref if present
        col_info = _resolve_ref(schema, col_info)

        python_enum = _field_python_enum(model_cls, col_name)
        sa_type = _map_to_sa_type(schema, col_info, col_name, python_enum)

        is_nullable = _resolve_sa_column_nullable(col_name, col_info, required_fields)

        fk_info = col_info.get("foreign_key") or {}
        column_unique = bool(col_info.get("unique")) or bool(fk_info.get("unique"))
        kwargs = {
            "primary_key": col_info.get("primary_key", False),
            "nullable": is_nullable,
            "unique": column_unique,
            "index": col_info.get("index", False),
        }

        args = [col_name, sa_type]

        # Handle Foreign Keys
        if fk_info:
            on_delete = fk_info.get("on_delete")
            args.append(sa.ForeignKey(f"{fk_info['to_table']}.id", ondelete=on_delete))

        columns.append(sa.Column(*args, **kwargs))

        if col_info.get("db_check"):
            enum_values_for_check = col_info.get("enum") or []
            db_check_columns.append((col_name, python_enum, list(enum_values_for_check)))

    table_args: list[Any] = list(columns)

    for col_name, python_enum, enum_values in db_check_columns:
        values = (
            [m.value for m in python_enum] if python_enum is not None else enum_values
        )
        if not values:
            continue
        rendered = ", ".join(
            (str(v) if isinstance(v, (int, float)) else f"'{str(v)}'") for v in values
        )
        sqltext = f"{col_name} IN ({rendered})"
        ck_name = _ck_constraint_name(table_name, col_name)
        table_args.append(sa.CheckConstraint(sqltext, name=ck_name))
    composites = schema.get("ferro_composite_uniques") or []
    for group in composites:
        if not isinstance(group, (list, tuple)) or len(group) < 2:
            warnings.warn(
                f"Ignoring invalid ferro_composite_uniques entry for table "
                f"{table_name!r} (expected a list/tuple of at least two column names): "
                f"{group!r}",
                UserWarning,
                stacklevel=2,
            )
            continue
        col_ids = [str(c) for c in group]
        uc_name = f"uq_{table_name}_{'_'.join(col_ids)}"
        if len(uc_name) > 63:
            uc_name = uc_name[:60] + "_uq"
        table_args.append(sa.UniqueConstraint(*col_ids, name=uc_name))

    composite_idxs = schema.get("ferro_composite_indexes") or []
    for group in composite_idxs:
        if not isinstance(group, (list, tuple)) or len(group) < 2:
            warnings.warn(
                f"Ignoring invalid ferro_composite_indexes entry for table "
                f"{table_name!r} (expected a list/tuple of at least two column names): "
                f"{group!r}",
                UserWarning,
                stacklevel=2,
            )
            continue
        col_ids = [str(c) for c in group]
        idx_name = f"idx_{table_name}_{'_'.join(col_ids)}"
        if len(idx_name) > 63:
            idx_name = idx_name[:59] + "_idx"
        table_args.append(sa.Index(idx_name, *col_ids))

    sa.Table(table_name, metadata, *table_args)


#: Canonical ``db_type`` token -> SA type. Duplicated on the Rust side in
#: ``src/schema.rs`` and pinned by the parity test (see U5). When adding a new
#: token, update both emitters in the same change. See AGENTS.md § I-1.
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

    match = _VARCHAR_RE.match(token)
    if match is not None:
        return sa.String(length=int(match.group(1)))
    return None


def _map_to_sa_type(
    schema: Dict[str, Any],
    col_info: Dict[str, Any],
    field_name: str,
    python_enum: type[enum.Enum] | None = None,
) -> "sa.types.TypeEngine":
    """Map Ferro/JSON schema types to SQLAlchemy types.

    ``field_name`` is used as the PostgreSQL enum type name when the column is
    not backed by a Python ``Enum`` subclass (for example join-table schemas
    built only from JSON schema). When ``python_enum`` is set, the type name is
    ``python_enum.__name__.lower()`` and member *values* are used as enum labels
    so string and integer Python enums map consistently.

    A ``db_type`` override on the JSON schema property takes precedence over
    every other branch -- it is the canonical user-facing storage knob and is
    validated at class-definition time (see ``metaclass._validate_db_type_options``).
    """
    warnings.warn(
        "_map_to_sa_type() is deprecated. Type lowering now flows through SchemaIR "
        "and _sa_type_from_ir_column(). Planned removal: v0.13.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    # Resolve $ref if present
    col_info = _resolve_ref(schema, col_info)

    db_type = col_info.get("db_type")
    if isinstance(db_type, str):
        mapped = _db_type_to_sa_type(db_type)
        if mapped is not None:
            return mapped

    json_type = col_info.get("type")
    format = col_info.get("format")
    enum_values = col_info.get("enum")

    # Handle Pydantic 'anyOf' for Optional types or Enums
    if "anyOf" in col_info:
        # Simple heuristic: find the first non-null type
        for item in col_info["anyOf"]:
            item = _resolve_ref(schema, item)
            if item.get("type") != "null":
                json_type = item.get("type")
                format = item.get("format") or format
                enum_values = item.get("enum") or enum_values
                break

    if enum_values:
        string_values = [str(v) for v in enum_values]
        if python_enum is not None:
            return sa.Enum(
                python_enum,
                name=python_enum.__name__.lower(),
                values_callable=lambda obj: [str(m.value) for m in obj],
            )
        return sa.Enum(*string_values, name=field_name)

    if json_type == "integer":
        return sa.Integer()
    elif json_type == "string":
        if format == "date-time":
            return sa.DateTime()
        elif format == "date":
            return sa.Date()
        elif format == "uuid":
            return sa.Uuid() if hasattr(sa, "Uuid") else sa.String(36)
        elif format == "decimal":
            return sa.Numeric()
        return sa.String()
    elif json_type == "boolean":
        return sa.Boolean()
    elif json_type == "number":
        # Check if it might be a decimal/numeric
        if format == "decimal":
            return sa.Numeric()
        return sa.Float()
    elif json_type == "object":
        return sa.JSON()
    elif json_type == "array":
        return sa.JSON()

    return sa.String()  # Fallback
