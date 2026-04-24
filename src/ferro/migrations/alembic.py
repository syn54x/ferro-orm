import enum
import types
import warnings
from typing import Annotated, Any, Dict, Union, get_args, get_origin

try:
    import sqlalchemy as sa
except ImportError:
    sa = None

from ..schema_metadata import build_model_schema
from ..state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY


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

    metadata = sa.MetaData()

    # 1. First, ensure all relationships are resolved
    from ..relations import resolve_relationships

    resolve_relationships()

    # 2. Process all registered models
    for model_name, model_cls in _MODEL_REGISTRY_PY.items():
        # Skip the base Model class
        if model_name == "Model":
            continue

        table_name = model_name.lower()

        try:
            schema = build_model_schema(model_cls)
        except Exception:
            # Fallback if standard pydantic schema fails (e.g. circular refs)
            continue

        _build_sa_table(metadata, table_name, schema, model_cls)

    # 3. Process join tables
    for join_table_name, join_schema in _JOIN_TABLE_REGISTRY.items():
        _build_sa_table(metadata, join_table_name, join_schema, model_cls=None)

    return metadata


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


def _field_python_enum(model_cls: type[Any] | None, field_name: str) -> type[enum.Enum] | None:
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
    columns = []

    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])

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

    table_args: list[Any] = list(columns)
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

    sa.Table(table_name, metadata, *table_args)


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
    """
    # Resolve $ref if present
    col_info = _resolve_ref(schema, col_info)

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
