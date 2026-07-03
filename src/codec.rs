//! Plan-driven value encoding and decoding between JSON, SeaQuery, and [`RustValue`].
//!
//! Centralizes bind-expression construction for INSERT/UPDATE/query paths and row decoding
//! after GIL-free fetch. Postgres-specific casts (UUID, enum UDT, temporal, JSON text) live
//! here so SQLite and Postgres stay observationally equivalent at the Python boundary.
//!
//! Every per-column type decision comes from the model's compiled
//! [`ModelCodecPlan`] (FF-C C1) — built once per model per schema epoch at
//! registration from the FF-B storage decision table. No JSON-schema shape or
//! pattern inference happens here.

use crate::backend::{EngineRow, EngineValue};
use crate::codec_plan::{ColumnCodec, ModelCodecPlan};
use crate::state::{Dialect, RustValue};
use chrono::SecondsFormat;
use sea_query::{Alias, Expr, SimpleExpr, Value as SeaValue};
use serde_json::Value;
use std::collections::{HashMap, HashSet};

/// One ORM row after GIL-free decode: optional stringified PK plus column values.
pub type ParsedRow = (Option<String>, Vec<(String, RustValue)>);

/// Postgres `CAST` target for temporal codecs (`None` for everything else).
fn temporal_cast_for_codec(codec: Option<&ColumnCodec>) -> Option<&'static str> {
    match codec {
        Some(ColumnCodec::DateTime) => Some("timestamptz"),
        Some(ColumnCodec::Date) => Some("date"),
        Some(ColumnCodec::Time) => Some("time"),
        _ => None,
    }
}

/// Whether the codec is integer-valued for bind purposes: the int family plus
/// enums whose member values are integers (`IntEnum`).
fn codec_is_integer(codec: Option<&ColumnCodec>) -> bool {
    match codec {
        Some(ColumnCodec::Int | ColumnCodec::SmallInt | ColumnCodec::BigInt) => true,
        Some(ColumnCodec::Enum { int_valued, .. }) => *int_valued,
        _ => false,
    }
}

/// Build a typed SeaQuery RHS expression for INSERT/UPDATE from JSON field values.
///
/// Uses the compiled codec plan plus live Postgres catalog hints (`enum_udt`,
/// `uuid_columns`, `ts_cast`) to emit OID-correct binds. See
/// `docs/solutions/patterns/typed-null-binds.md`.
///
/// # Arguments
/// * `plan` — Compiled codec plan for the model.
/// * `table_name` — Table name (for UUID parse error messages).
/// * `col_name` — Target column.
/// * `value` — JSON value from the Python layer (`null` for SQL `NULL`).
/// * `enum_udt` — Native Postgres enum type names by column.
/// * `uuid_columns` — Columns stored as SQL `uuid` on Postgres.
/// * `ts_cast` — Per-column `CAST` target for date/timestamp families.
/// * `backend` — Active SQL dialect.
///
/// # Returns
/// A SeaQuery `SimpleExpr` suitable for `.values([...])` or `.set(...)`.
///
/// # Errors
/// Returns `PyValueError` when a Postgres UUID string fails to parse.
#[allow(clippy::too_many_arguments)]
pub fn schema_bind_expr(
    plan: &ModelCodecPlan,
    table_name: &str,
    col_name: &str,
    value: &Value,
    enum_udt: &HashMap<String, String>,
    uuid_columns: &HashSet<String>,
    ts_cast: &HashMap<String, String>,
    backend: Dialect,
) -> pyo3::PyResult<SimpleExpr> {
    let codec = plan.codec(col_name);
    let is_uuid_pg = backend == Dialect::Postgres
        && (uuid_columns.contains(col_name) || matches!(codec, Some(ColumnCodec::Uuid)));

    if backend == Dialect::Postgres
        && let Some(tn) = crate::schema_bind::native_postgres_enum_udt_name(col_name, enum_udt)
    {
        match value {
            Value::String(s) => {
                return Ok(crate::schema_bind::postgres_enum_string_rhs_expr(s, tn));
            }
            // Nullable enum column: an untyped NULL binds as text, which a
            // prepared INSERT rejects against the enum column — cast it.
            Value::Null => {
                return Ok(Expr::value(SeaValue::String(None)).cast_as(Alias::new(tn)));
            }
            // IntEnum members arrive as JSON numbers; the native enum's labels
            // are their stringified values ("1", "2", ...).
            Value::Number(n) => {
                return Ok(crate::schema_bind::postgres_enum_string_rhs_expr(
                    &n.to_string(),
                    tn,
                ));
            }
            _ => {}
        }
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
        .or_else(|| temporal_cast_for_codec(codec));
    if backend == Dialect::Postgres
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
            if backend == Dialect::Postgres && matches!(codec, Some(ColumnCodec::Json)) =>
        {
            if value.is_null() {
                Expr::value(SeaValue::String(None)).cast_as("json")
            } else {
                Expr::value(SeaValue::String(Some(Box::new(value.to_string())))).cast_as("json")
            }
        }
        Value::String(s) if codec_is_integer(codec) => {
            if let Ok(parsed) = s.parse::<i64>() {
                Expr::value(SeaValue::BigInt(Some(parsed)))
            } else {
                Expr::value(SeaValue::String(Some(Box::new(s.clone()))))
            }
        }
        Value::String(s) if matches!(codec, Some(ColumnCodec::Float)) => {
            if let Ok(parsed) = s.parse::<f64>() {
                Expr::value(SeaValue::Double(Some(parsed)))
            } else {
                Expr::value(SeaValue::String(Some(Box::new(s.clone()))))
            }
        }
        Value::String(s) if matches!(codec, Some(ColumnCodec::Bytes)) => {
            Expr::value(SeaValue::Bytes(Some(Box::new(s.as_bytes().to_vec()))))
        }
        Value::String(s) if matches!(codec, Some(ColumnCodec::Decimal)) => {
            if backend == Dialect::Postgres {
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
        Value::Bool(b)
            if matches!(codec, Some(ColumnCodec::Bool)) && backend == Dialect::Sqlite =>
        {
            Expr::value(SeaValue::BigInt(Some(if *b { 1 } else { 0 })))
        }
        Value::Bool(b) => Expr::value(SeaValue::Bool(Some(*b))),
        Value::Null => {
            if matches!(codec, Some(ColumnCodec::Decimal)) && backend == Dialect::Postgres {
                return Ok(Expr::value(SeaValue::String(None)).cast_as("numeric"));
            }
            Expr::value(typed_null_for_codec(codec))
        }
        _ => Expr::value(SeaValue::String(Some(Box::new(value.to_string())))),
    };
    Ok(expr)
}

/// Typed SeaQuery `NULL` for a codec so `Option::<T>::None` reaches the wire
/// with the right OID on strict-typing backends.
fn typed_null_for_codec(codec: Option<&ColumnCodec>) -> SeaValue {
    match codec {
        Some(ColumnCodec::Bytes) => SeaValue::Bytes(None),
        Some(ColumnCodec::Decimal) => SeaValue::Double(None),
        Some(ColumnCodec::Float) => SeaValue::Double(None),
        Some(ColumnCodec::Bool) => SeaValue::Bool(None),
        codec if codec_is_integer(codec) => SeaValue::BigInt(None),
        _ => SeaValue::String(None),
    }
}

/// Build a typed SeaQuery RHS for WHERE-clause predicates.
///
/// Differs from [`schema_bind_expr`] in that enum casting uses catalog introspection only
/// (not schema `enum_type_name`) so auto-migrated TEXT enum columns keep text binds.
///
/// # Arguments
/// * `plan` — Compiled codec plan (`None` when the model is not registered).
/// * `col_name` — Filtered column.
/// * `val` — JSON RHS from the query IR.
/// * `infer_uuid_without_schema` — When true, parse UUID strings even if the plan lacks the column.
/// * `backend` — Active SQL dialect.
/// * `postgres_enum_udt` — Native enum UDT names from `pg_catalog`.
///
/// # Returns
/// A SeaQuery expression for the predicate RHS.
pub fn query_bind_expr(
    plan: Option<&ModelCodecPlan>,
    col_name: &str,
    val: &Value,
    infer_uuid_without_schema: bool,
    backend: Dialect,
    postgres_enum_udt: &HashMap<String, String>,
) -> SimpleExpr {
    let codec = plan.and_then(|p| p.codec(col_name));
    let col_is_uuid = matches!(codec, Some(ColumnCodec::Uuid));
    let col_is_decimal = matches!(codec, Some(ColumnCodec::Decimal));

    if let Value::String(s) = val {
        if backend == Dialect::Postgres {
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

            if let Some(cast) = temporal_cast_for_codec(codec) {
                return Expr::value(SeaValue::String(Some(Box::new(s.clone())))).cast_as(cast);
            }
            if matches!(codec, Some(ColumnCodec::Bytes)) {
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
        if backend == Dialect::Postgres {
            if col_is_uuid {
                return Expr::value(SeaValue::Uuid(None));
            }
            if let Some(cast) = temporal_cast_for_codec(codec) {
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
        if matches!(codec, Some(ColumnCodec::Bytes)) {
            return Expr::value(SeaValue::Bytes(None));
        }
        return Expr::value(typed_null_for_codec(codec));
    }

    Expr::value(json_value_to_sea_value(val))
}

/// Wrap a many-to-many join-column bind with Postgres UUID typing when needed.
///
/// # Arguments
/// * `col_name` — Join table column (`source_id` or `target_id`).
/// * `value` — SeaQuery value produced from the Python ID.
/// * `uuid_columns` — UUID-typed columns on the join table (Postgres catalog).
/// * `backend` — Active SQL dialect.
///
/// # Returns
/// Expression with `Value::Uuid` when the column is a Postgres UUID; otherwise passes `value` through.
pub fn m2m_bind_expr(
    col_name: &str,
    value: SeaValue,
    uuid_columns: &HashSet<String>,
    backend: Dialect,
) -> SimpleExpr {
    if backend == Dialect::Postgres && uuid_columns.contains(col_name) {
        if let SeaValue::String(Some(s)) = &value
            && let Ok(parsed) = uuid::Uuid::parse_str(s)
        {
            return Expr::value(SeaValue::Uuid(Some(Box::new(parsed))));
        }
        return Expr::value(value).cast_as("uuid");
    }
    Expr::value(value)
}

/// Coerce a JSON literal into a SeaQuery `Value` without column context.
///
/// Used as the fallback arm of [`query_bind_expr`] for non-null primitives.
/// JSON `null` maps to `String(None)` (untyped null) — prefer plan-aware paths for NULL.
///
/// # Arguments
/// * `value` — JSON value from the query IR.
///
/// # Returns
/// Best-effort SeaQuery `Value` (`BigInt`, `Double`, `Bool`, `String`, or untyped null).
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

/// Decode one [`EngineValue`] into a [`RustValue`] using the compiled codec plan.
///
/// Typed wire values (native Postgres decode, FF-C C3) carry the physical
/// truth of the column; the plan refines the Python-facing shape (e.g. a
/// `UUID` column widened to text storage still hydrates `uuid.UUID`, and a
/// `str` column widened to `uuid` storage hydrates `str`). SQLite's text wire
/// funnels into the same shapes, keeping both backends observationally
/// identical at the Python boundary. Columns outside the plan (joined/m2m
/// columns) take each wire value's natural mapping.
///
/// # Arguments
/// * `value` — Wire value from SQLx fetch.
/// * `plan` — Compiled codec plan for the model.
/// * `col_name` — Column being decoded.
///
/// # Returns
/// Rust-native value ready for [`RustValue::into_py_any`].
pub fn decode_engine_value(value: EngineValue, plan: &ModelCodecPlan, col_name: &str) -> RustValue {
    let codec = plan.codec(col_name);
    let plan_says_str = matches!(codec, Some(ColumnCodec::Str));

    // Typed wire values: natural mapping unless the plan's logical codec is
    // `Str` (explicit `db_type` widened storage away from a str field).
    match value {
        EngineValue::Uuid(v) => {
            let s = v.hyphenated().to_string();
            return if plan_says_str {
                RustValue::String(s)
            } else {
                RustValue::Uuid(s)
            };
        }
        EngineValue::TimestampTz(v) => {
            let s = v.to_rfc3339_opts(SecondsFormat::Micros, false);
            return if plan_says_str {
                RustValue::String(s)
            } else {
                RustValue::DateTime(s)
            };
        }
        EngineValue::Timestamp(v) => {
            let s = v.format("%Y-%m-%dT%H:%M:%S%.6f").to_string();
            return if plan_says_str {
                RustValue::String(s)
            } else {
                RustValue::DateTime(s)
            };
        }
        EngineValue::Date(v) => {
            let s = v.to_string();
            return if plan_says_str {
                RustValue::String(s)
            } else {
                RustValue::Date(s)
            };
        }
        EngineValue::Time(v) => {
            let s = v.format("%H:%M:%S%.6f").to_string();
            return if plan_says_str {
                RustValue::String(s)
            } else {
                RustValue::Time(s)
            };
        }
        EngineValue::Decimal(v) => {
            return if plan_says_str {
                RustValue::String(v)
            } else {
                RustValue::Decimal(v)
            };
        }
        EngineValue::Json(v) => {
            return if plan_says_str {
                RustValue::String(v.to_string())
            } else {
                RustValue::Json(v)
            };
        }
        _ => {}
    }

    if matches!(codec, Some(ColumnCodec::Decimal)) {
        return match value {
            EngineValue::I64(v) => RustValue::Decimal(v.to_string()),
            EngineValue::F64(v) => RustValue::Decimal(v.to_string()),
            EngineValue::String(v) => RustValue::Decimal(v),
            _ => RustValue::None,
        };
    }

    if matches!(codec, Some(ColumnCodec::Bytes)) {
        return match value {
            EngineValue::Bytes(v) => RustValue::Blob(v),
            EngineValue::String(v) => RustValue::Blob(v.into_bytes()),
            _ => RustValue::None,
        };
    }

    match value {
        EngineValue::I64(v) if matches!(codec, Some(ColumnCodec::Bool)) => RustValue::Bool(v != 0),
        EngineValue::I64(v) => RustValue::BigInt(v),
        EngineValue::F64(v) => RustValue::Double(v),
        EngineValue::Bytes(v) => RustValue::Blob(v),
        EngineValue::String(v) => match codec {
            Some(ColumnCodec::DateTime) => RustValue::DateTime(v),
            Some(ColumnCodec::Date) => RustValue::Date(v),
            Some(ColumnCodec::Time) => RustValue::Time(v),
            Some(ColumnCodec::Uuid) => RustValue::Uuid(v),
            // IntEnum members are stored as their stringified values on
            // text-family storage; hydration needs the integer back so the
            // Python enum class can resolve the member (FF-C C4, F14).
            Some(ColumnCodec::Enum {
                int_valued: true, ..
            }) => match v.parse::<i64>() {
                Ok(parsed) => RustValue::BigInt(parsed),
                Err(_) => RustValue::String(v),
            },
            Some(ColumnCodec::Json) => {
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
        // Typed variants are fully handled above.
        _ => RustValue::None,
    }
}

/// Decode a batch of [`EngineRow`]s into GIL-free [`ParsedRow`] data.
///
/// # Arguments
/// * `rows` — Raw rows from the engine.
/// * `plan` — Compiled codec plan for per-column decoding.
/// * `pk_col` — Primary key column name when known (extracts stringified PK per row).
///
/// # Returns
/// One `(pk, fields)` tuple per input row.
pub fn typed_rows_to_parsed_data(
    rows: Vec<EngineRow>,
    plan: &ModelCodecPlan,
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
                        // Native uuid PKs stringify to the same canonical
                        // lowercase-hyphenated form SQLite stores as text, so
                        // identity-map keys agree across backends.
                        EngineValue::Uuid(v) => Some(v.hyphenated().to_string()),
                        _ => None,
                    };
                }
                let value = decode_engine_value(value, plan, &col_name);
                fields.push((col_name, value));
            }

            (row_pk_val, fields)
        })
        .collect()
}
