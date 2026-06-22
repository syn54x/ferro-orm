use crate::backend::{EngineRow, EngineValue};
use crate::state::{MODEL_REGISTRY, RustValue, SqlDialect};
use sea_query::{Alias, Expr, SelectStatement, SimpleExpr, Value as SeaValue};
use serde_json::Value;
use std::collections::{HashMap, HashSet};

pub type ParsedRow = (Option<String>, Vec<(String, RustValue)>);

fn json_type(col_info: &Value) -> Option<&str> {
    col_info.get("type").and_then(|t| t.as_str()).or_else(|| {
        col_info
            .get("anyOf")
            .and_then(|a| a.as_array())
            .and_then(|types| {
                types.iter().find_map(|t| {
                    let s = t.get("type")?.as_str()?;
                    if s == "null" { None } else { Some(s) }
                })
            })
    })
}

fn format(col_info: &Value) -> Option<&str> {
    col_info.get("format").and_then(|f| f.as_str()).or_else(|| {
        col_info
            .get("anyOf")
            .and_then(|a| a.as_array())
            .and_then(|types| {
                types.iter().find_map(|t| {
                    let ty = t.get("type")?.as_str()?;
                    if ty == "null" {
                        None
                    } else {
                        t.get("format").and_then(|f| f.as_str())
                    }
                })
            })
    })
}

fn is_decimal(col_info: &Value) -> bool {
    col_info
        .get("anyOf")
        .and_then(|a| a.as_array())
        .map(|types| {
            let has_number = types
                .iter()
                .any(|t| t.get("type").and_then(|ty| ty.as_str()) == Some("number"));
            let has_patterned_string = types.iter().any(|t| {
                t.get("type").and_then(|ty| ty.as_str()) == Some("string")
                    && t.get("pattern").is_some()
            });
            has_number && has_patterned_string
        })
        .unwrap_or(false)
}

fn is_enum(col_info: &Value) -> bool {
    col_info.get("enum").and_then(|e| e.as_array()).is_some()
}

fn resolve_ref<'a>(schema: &'a Value, col_info: &'a Value) -> &'a Value {
    if let Some(ref_path) = col_info.get("$ref").and_then(|r| r.as_str())
        && let Some(def_name) = ref_path.strip_prefix("#/$defs/")
        && let Some(def) = schema.get("$defs").and_then(|defs| defs.get(def_name))
    {
        return def;
    }
    col_info
}

fn schema_property<'a>(schema: &'a Value, col_name: &str) -> Option<&'a Value> {
    schema
        .get("properties")
        .and_then(|p| p.get(col_name))
        .map(|prop| resolve_ref(schema, prop))
}

fn temporal_cast_for_format(fmt: Option<&str>) -> Option<&'static str> {
    match fmt {
        Some("date-time") => Some("timestamptz"),
        Some("date") => Some("date"),
        Some("time") => Some("time"),
        _ => None,
    }
}

fn model_schema_property(model_name: &str, col_name: &str) -> Option<Value> {
    let registry = MODEL_REGISTRY.read().ok()?;
    let schema = registry.get(model_name)?;
    let col_info = schema
        .get("properties")
        .and_then(|p| p.get(col_name))
        .map(|prop| resolve_ref(schema, prop))?;
    Some(col_info.clone())
}

pub fn apply_postgres_text_select_columns(
    select: &mut SelectStatement,
    table_name: &str,
    schema: &Value,
    pg_native_enum_columns: &HashSet<String>,
    backend: SqlDialect,
) {
    let tbl = Alias::new(table_name);
    if backend != SqlDialect::Postgres {
        select.column((tbl.clone(), sea_query::Asterisk));
        return;
    }
    let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) else {
        select.column((tbl.clone(), sea_query::Asterisk));
        return;
    };
    let need_text_from_schema = properties.values().any(|col_info| {
        let resolved = resolve_ref(schema, col_info);
        matches!(
            format(resolved),
            Some("uuid" | "date-time" | "date" | "decimal")
        ) || matches!(json_type(resolved), Some("object" | "array"))
            || is_enum(resolved)
    });
    let need_text_from_native_enum = properties
        .keys()
        .any(|k| pg_native_enum_columns.contains(k.as_str()));
    if !need_text_from_schema && !need_text_from_native_enum {
        select.column((tbl.clone(), sea_query::Asterisk));
        return;
    }
    for (col_name, col_info) in properties {
        let col_iden = Alias::new(col_name.as_str());
        let col_info = resolve_ref(schema, col_info);
        if matches!(
            format(col_info),
            Some("uuid" | "date-time" | "date" | "decimal")
        ) || matches!(json_type(col_info), Some("object" | "array"))
            || is_enum(col_info)
            || pg_native_enum_columns.contains(col_name.as_str())
        {
            let expr = Expr::cast_as(
                Expr::col((tbl.clone(), col_iden.clone())),
                Alias::new("text"),
            );
            select.expr_as(expr, col_iden);
        } else {
            select.column((tbl.clone(), col_iden));
        }
    }
}

#[allow(clippy::too_many_arguments)]
pub fn schema_bind_expr(
    schema: &Value,
    table_name: &str,
    col_name: &str,
    value: &Value,
    enum_udt: &HashMap<String, String>,
    uuid_columns: &HashSet<String>,
    ts_cast: &HashMap<String, String>,
    backend: SqlDialect,
) -> pyo3::PyResult<SimpleExpr> {
    let col_info = schema_property(schema, col_name);
    let col_format = col_info.and_then(format);
    let col_json_type = col_info.and_then(json_type);
    let col_is_decimal = col_info.map(is_decimal).unwrap_or(false);
    let is_uuid_pg = backend == SqlDialect::Postgres
        && (uuid_columns.contains(col_name) || col_format == Some("uuid"));

    if let Value::String(s) = value
        && backend == SqlDialect::Postgres
        && let Some(tn) =
            crate::schema_bind::postgres_enum_type_name_for_column(col_name, enum_udt, col_info)
    {
        return Ok(crate::schema_bind::postgres_enum_string_rhs_expr(s, &tn));
    }

    if is_uuid_pg {
        return match value {
            Value::Null => Ok(Expr::value(SeaValue::Uuid(None))),
            Value::String(s) => {
                let parsed = uuid::Uuid::parse_str(s).map_err(|_| {
                    pyo3::exceptions::PyValueError::new_err(format!(
                        "Invalid UUID for {table_name}.{col_name}: {s}"
                    ))
                })?;
                Ok(Expr::value(SeaValue::Uuid(Some(Box::new(parsed)))))
            }
            _ => Ok(Expr::value(SeaValue::String(Some(Box::new(
                value.to_string(),
            ))))),
        };
    }

    let temporal_cast = ts_cast
        .get(col_name)
        .map(|s| s.as_str())
        .or_else(|| temporal_cast_for_format(col_format));
    if backend == SqlDialect::Postgres
        && let Some(cast) = temporal_cast
    {
        if value.is_null() {
            return Ok(Expr::value(SeaValue::String(None)).cast_as(Alias::new(cast)));
        }
        if let Value::String(s) = value {
            return Ok(
                Expr::value(SeaValue::String(Some(Box::new(s.clone())))).cast_as(Alias::new(cast))
            );
        }
    }

    let expr = match value {
        value
            if backend == SqlDialect::Postgres
                && matches!(col_json_type, Some("object" | "array")) =>
        {
            if value.is_null() {
                Expr::value(SeaValue::String(None)).cast_as("json")
            } else {
                Expr::value(SeaValue::String(Some(Box::new(value.to_string())))).cast_as("json")
            }
        }
        Value::String(s) if col_json_type == Some("integer") => {
            if let Ok(parsed) = s.parse::<i64>() {
                Expr::value(SeaValue::BigInt(Some(parsed)))
            } else {
                Expr::value(SeaValue::String(Some(Box::new(s.clone()))))
            }
        }
        Value::String(s) if col_json_type == Some("number") => {
            if let Ok(parsed) = s.parse::<f64>() {
                Expr::value(SeaValue::Double(Some(parsed)))
            } else {
                Expr::value(SeaValue::String(Some(Box::new(s.clone()))))
            }
        }
        Value::String(s) if col_format == Some("binary") => {
            Expr::value(SeaValue::Bytes(Some(Box::new(s.as_bytes().to_vec()))))
        }
        Value::String(s) if col_is_decimal => {
            if backend == SqlDialect::Postgres {
                Expr::value(SeaValue::String(Some(Box::new(s.clone())))).cast_as("numeric")
            } else if let Ok(parsed) = s.parse::<f64>() {
                Expr::value(SeaValue::Double(Some(parsed)))
            } else {
                Expr::value(SeaValue::String(Some(Box::new(s.clone()))))
            }
        }
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Expr::value(SeaValue::BigInt(Some(i)))
            } else if let Some(f) = n.as_f64() {
                Expr::value(SeaValue::Double(Some(f)))
            } else {
                Expr::value(SeaValue::String(None))
            }
        }
        Value::String(s) => Expr::value(SeaValue::String(Some(Box::new(s.clone())))),
        Value::Bool(b) if col_json_type == Some("boolean") && backend == SqlDialect::Sqlite => {
            Expr::value(SeaValue::BigInt(Some(if *b { 1 } else { 0 })))
        }
        Value::Bool(b) => Expr::value(SeaValue::Bool(Some(*b))),
        Value::Null => {
            let v = if col_format == Some("binary") {
                SeaValue::Bytes(None)
            } else if col_is_decimal {
                if backend == SqlDialect::Postgres {
                    SeaValue::String(None)
                } else {
                    SeaValue::Double(None)
                }
            } else if backend == SqlDialect::Postgres && temporal_cast.is_some() {
                SeaValue::String(None)
            } else {
                match col_json_type {
                    Some("integer") => SeaValue::BigInt(None),
                    Some("number") => SeaValue::Double(None),
                    Some("boolean") => SeaValue::Bool(None),
                    Some("string") => SeaValue::String(None),
                    _ => SeaValue::String(None),
                }
            };
            Expr::value(v)
        }
        _ => Expr::value(SeaValue::String(Some(Box::new(value.to_string())))),
    };
    Ok(expr)
}

pub fn query_bind_expr(
    model_name: &str,
    col_name: &str,
    val: &Value,
    infer_uuid_without_schema: bool,
    backend: SqlDialect,
    postgres_enum_udt: &HashMap<String, String>,
) -> SimpleExpr {
    let col_info = model_schema_property(model_name, col_name);
    let col_format = col_info.as_ref().and_then(format);
    let col_is_decimal = col_info.as_ref().map(is_decimal).unwrap_or(false);
    let col_is_uuid = col_info
        .as_ref()
        .map(|c| json_type(c) == Some("string") && format(c) == Some("uuid"))
        .unwrap_or(false);

    if let Value::String(s) = val {
        if backend == SqlDialect::Postgres {
            if let Some(tn) =
                crate::schema_bind::native_postgres_enum_udt_name(col_name, postgres_enum_udt)
            {
                return crate::schema_bind::postgres_enum_string_rhs_expr(s, tn);
            }

            if let Ok(parsed) = uuid::Uuid::parse_str(s)
                && (col_is_uuid || infer_uuid_without_schema)
            {
                return Expr::value(SeaValue::Uuid(Some(Box::new(parsed))));
            }

            if let Some(cast) = temporal_cast_for_format(col_format) {
                return Expr::value(SeaValue::String(Some(Box::new(s.clone())))).cast_as(cast);
            }
            if col_format == Some("binary") {
                return Expr::value(SeaValue::Bytes(Some(Box::new(s.as_bytes().to_vec()))));
            }
            if col_is_decimal {
                return Expr::value(SeaValue::String(Some(Box::new(s.clone())))).cast_as("numeric");
            }
        }

        if col_is_decimal && let Ok(parsed) = s.parse::<f64>() {
            return Expr::value(SeaValue::Double(Some(parsed)));
        }
    }

    if val.is_null() {
        if backend == SqlDialect::Postgres {
            if col_is_uuid {
                return Expr::value(SeaValue::Uuid(None));
            }
            if let Some(cast) = temporal_cast_for_format(col_format) {
                return Expr::value(SeaValue::String(None)).cast_as(cast);
            }
            if col_is_decimal {
                return Expr::value(SeaValue::String(None)).cast_as("numeric");
            }
        }
        if col_is_uuid {
            return Expr::value(SeaValue::Uuid(None));
        }
        if col_is_decimal {
            return Expr::value(SeaValue::Double(None));
        }
        if col_format == Some("binary") {
            return Expr::value(SeaValue::Bytes(None));
        }
        let col_json_type = col_info.as_ref().and_then(json_type);
        let typed_null = match col_json_type {
            Some("integer") => SeaValue::BigInt(None),
            Some("number") => SeaValue::Double(None),
            Some("boolean") => SeaValue::Bool(None),
            Some("string") => SeaValue::String(None),
            _ => SeaValue::String(None),
        };
        return Expr::value(typed_null);
    }

    Expr::value(json_value_to_sea_value(val))
}

pub fn m2m_bind_expr(
    col_name: &str,
    value: SeaValue,
    uuid_columns: &HashSet<String>,
    backend: SqlDialect,
) -> SimpleExpr {
    if backend == SqlDialect::Postgres && uuid_columns.contains(col_name) {
        if let SeaValue::String(Some(s)) = &value
            && let Ok(parsed) = uuid::Uuid::parse_str(s)
        {
            return Expr::value(SeaValue::Uuid(Some(Box::new(parsed))));
        }
        return Expr::value(value).cast_as("uuid");
    }
    Expr::value(value)
}

pub fn json_value_to_sea_value(value: &Value) -> SeaValue {
    match value {
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                SeaValue::BigInt(Some(i))
            } else if let Some(f) = n.as_f64() {
                SeaValue::Double(Some(f))
            } else {
                SeaValue::String(None)
            }
        }
        Value::String(s) => SeaValue::String(Some(Box::new(s.clone()))),
        Value::Bool(b) => SeaValue::Bool(Some(*b)),
        Value::Null => SeaValue::String(None),
        _ => SeaValue::String(Some(Box::new(value.to_string()))),
    }
}

pub fn decode_engine_value(value: EngineValue, schema: &Value, col_name: &str) -> RustValue {
    let prop = schema
        .get("properties")
        .and_then(|p| p.get(col_name))
        .map(|col_info| resolve_ref(schema, col_info));

    let col_format = prop.and_then(format);
    let col_is_decimal = prop.map(is_decimal).unwrap_or(false);
    let col_json_type = prop.and_then(json_type);

    if col_is_decimal {
        return match value {
            EngineValue::I64(v) => RustValue::Decimal(v.to_string()),
            EngineValue::F64(v) => RustValue::Decimal(v.to_string()),
            EngineValue::String(v) => RustValue::Decimal(v),
            _ => RustValue::None,
        };
    }

    if col_format == Some("binary") {
        return match value {
            EngineValue::Bytes(v) => RustValue::Blob(v),
            EngineValue::String(v) => RustValue::Blob(v.into_bytes()),
            _ => RustValue::None,
        };
    }

    match value {
        EngineValue::I64(v) if col_json_type == Some("boolean") => RustValue::Bool(v != 0),
        EngineValue::I64(v) => RustValue::BigInt(v),
        EngineValue::F64(v) => RustValue::Double(v),
        EngineValue::Bytes(v) => RustValue::Blob(v),
        EngineValue::String(v) => match (col_json_type, col_format) {
            (_, Some("date-time")) => RustValue::DateTime(v),
            (_, Some("date")) => RustValue::Date(v),
            (_, Some("uuid")) => RustValue::Uuid(v),
            (Some("object"), _) | (Some("array"), _) => {
                if let Ok(json_val) = serde_json::from_str(&v) {
                    RustValue::Json(json_val)
                } else {
                    RustValue::String(v)
                }
            }
            _ => RustValue::String(v),
        },
        EngineValue::Bool(v) => RustValue::Bool(v),
        EngineValue::Null => RustValue::None,
    }
}

pub fn typed_rows_to_parsed_data(
    rows: Vec<EngineRow>,
    schema: &Value,
    pk_col: Option<&str>,
) -> Vec<ParsedRow> {
    rows.into_iter()
        .map(|row| {
            let mut row_pk_val = None;
            let mut fields = Vec::with_capacity(row.values.len());

            for (col_name, value) in row.values {
                if pk_col == Some(col_name.as_str()) {
                    row_pk_val = match &value {
                        EngineValue::I64(v) => Some(v.to_string()),
                        EngineValue::String(v) => Some(v.clone()),
                        _ => None,
                    };
                }
                let value = decode_engine_value(value, schema, &col_name);
                fields.push((col_name, value));
            }

            (row_pk_val, fields)
        })
        .collect()
}
