from typing import Any, Dict

try:
    import sqlalchemy as sa
except ImportError:
    sa = None

from ..state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY


def get_metadata() -> "sa.MetaData":
    """
    Generate a SQLAlchemy MetaData object representing all registered Ferro models.
    This is intended to be used in alembic's env.py for autogenerate support.
    """
    if sa is None:
        raise ImportError(
            "SQLAlchemy is required to use the alembic bridge. "
            "Install it via 'pip install ferro[alembic]'."
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

        # Get the schema that was registered with Rust
        try:
            # Generate schema with internal references for resolution
            schema = model_cls.model_json_schema()
        except Exception:
            # Fallback if standard pydantic schema fails (e.g. circular refs)
            continue

        # Enrich schema with Ferro-specific metadata (PKs, FKs, etc.)
        _enrich_schema_with_ferro_metadata(model_cls, schema)

        _build_sa_table(metadata, table_name, schema)

    # 3. Process join tables
    for join_table_name, join_schema in _JOIN_TABLE_REGISTRY.items():
        _build_sa_table(metadata, join_table_name, join_schema)

    return metadata


def _resolve_ref(schema: Dict[str, Any], col_info: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve $ref in JSON schema if present."""
    if "$ref" in col_info:
        ref_path = col_info["$ref"]
        if ref_path.startswith("#/$defs/"):
            def_name = ref_path.split("/")[-1]
            return schema.get("$defs", {}).get(def_name, col_info)
    return col_info


def _enrich_schema_with_ferro_metadata(model_cls, schema: Dict[str, Any]):
    """Enrich the Pydantic schema with Ferro-specific metadata like PKs and FKs."""
    if "properties" not in schema:
        return

    # Apply FerroField metadata (PK, Unique, Index)
    for f_name, metadata in model_cls.ferro_fields.items():
        if f_name in schema["properties"]:
            schema["properties"][f_name]["primary_key"] = metadata.primary_key
            schema["properties"][f_name]["unique"] = metadata.unique
            schema["properties"][f_name]["index"] = metadata.index

    # Apply ForeignKey metadata
    for f_name, metadata in model_cls.ferro_relations.items():
        from ..base import ForeignKey

        if isinstance(metadata, ForeignKey):
            id_field = f"{f_name}_id"
            if id_field in schema["properties"]:
                target_name = (
                    metadata.to.__name__
                    if hasattr(metadata.to, "__name__")
                    else str(metadata.to)
                )
                schema["properties"][id_field]["foreign_key"] = {
                    "to_table": target_name.lower(),
                    "on_delete": metadata.on_delete,
                    "unique": metadata.unique,
                }


def _build_sa_table(metadata: "sa.MetaData", table_name: str, schema: Dict[str, Any]):
    """Build a SQLAlchemy Table object from a Ferro JSON schema."""
    columns = []

    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])

    for col_name, col_info in properties.items():
        # Resolve $ref if present
        col_info = _resolve_ref(schema, col_info)

        sa_type = _map_to_sa_type(schema, col_info)

        # Better nullability detection
        is_nullable = True

        # 1. If it's in required, it's definitely not nullable
        if col_name in required_fields:
            is_nullable = False

        # 2. Check for explicit null in anyOf
        if "anyOf" in col_info:
            has_null = any(item.get("type") == "null" for item in col_info["anyOf"])
            if not has_null:
                # If there's an anyOf but none of them are null, it's not nullable
                is_nullable = False
            else:
                is_nullable = True
        elif col_info.get("type") == "null":
            is_nullable = True
        elif "type" in col_info and col_info["type"] != "null":
            # If it has a single type that is not null, and it's not in anyOf
            # We still respect 'required_fields' for the 'optional' case
            pass

        # 3. Special case: if it has a default value that is not None,
        # it is often intended to be NOT NULL in the DB with a default.
        if "default" in col_info and col_info["default"] is not None:
            is_nullable = False

        kwargs = {
            "primary_key": col_info.get("primary_key", False),
            "nullable": is_nullable,
            "unique": col_info.get("unique", False),
            "index": col_info.get("index", False),
        }

        # For primary keys, we often want nullable=False explicitly
        if kwargs["primary_key"]:
            kwargs["nullable"] = False

        args = [col_name, sa_type]

        # Handle Foreign Keys
        if "foreign_key" in col_info:
            fk_info = col_info["foreign_key"]
            on_delete = fk_info.get("on_delete")
            args.append(sa.ForeignKey(f"{fk_info['to_table']}.id", ondelete=on_delete))

        columns.append(sa.Column(*args, **kwargs))

    sa.Table(table_name, metadata, *columns)


def _map_to_sa_type(
    schema: Dict[str, Any], col_info: Dict[str, Any]
) -> "sa.types.TypeEngine":
    """Map Ferro/JSON schema types to SQLAlchemy types."""
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
                format = item.get("format")
                enum_values = item.get("enum") or enum_values
                break

    if enum_values:
        return sa.Enum(*enum_values)

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
