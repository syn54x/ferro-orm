use crate::BackendDialect;
use ferro_schema_ir::SchemaColumn;
use sea_query::{ColumnDef, Value};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CanonicalType {
    Integer,
    SmallInt,
    BigInt,
    Double,
    Decimal,
    Boolean,
    Json,
    Text,
    Varchar(Option<u32>),
    Char(u32),
    Uuid,
    DateTime,
    Timestamp,
    TimestampTz,
    Date,
    Time,
    Blob,
}

pub fn apply_canonical_type(col_def: &mut ColumnDef, canonical: CanonicalType) {
    match canonical {
        CanonicalType::Integer => col_def.integer(),
        CanonicalType::SmallInt => col_def.small_integer(),
        CanonicalType::BigInt => col_def.big_integer(),
        CanonicalType::Double => col_def.double(),
        CanonicalType::Decimal => col_def.decimal(),
        CanonicalType::Boolean => col_def.boolean(),
        CanonicalType::Json => col_def.json(),
        CanonicalType::Text => col_def.text(),
        CanonicalType::Varchar(None) => col_def.string(),
        CanonicalType::Varchar(Some(n)) => col_def.string_len(n),
        CanonicalType::Char(n) => col_def.char_len(n),
        CanonicalType::Uuid => col_def.uuid(),
        CanonicalType::DateTime => col_def.date_time(),
        CanonicalType::Timestamp => col_def.timestamp(),
        CanonicalType::TimestampTz => col_def.timestamp_with_time_zone(),
        CanonicalType::Date => col_def.date(),
        CanonicalType::Time => col_def.time(),
        CanonicalType::Blob => col_def.blob(),
    };
}

fn json_type_to_canonical(json_type: &str, backend: BackendDialect) -> CanonicalType {
    match json_type {
        "integer" => CanonicalType::Integer,
        "string" => CanonicalType::Varchar(None),
        "number" => CanonicalType::Double,
        "boolean" => match backend {
            BackendDialect::Sqlite => CanonicalType::Integer,
            BackendDialect::Postgres => CanonicalType::Boolean,
        },
        "object" | "array" => CanonicalType::Json,
        _ => CanonicalType::Varchar(None),
    }
}

pub fn parse_varchar_token(token: &str) -> Option<u32> {
    let body = token.strip_prefix("varchar(")?.strip_suffix(')')?;
    let n: u32 = body.parse().ok()?;
    if n == 0 { None } else { Some(n) }
}

pub fn db_type_token_to_canonical(token: &str, backend: BackendDialect) -> Option<CanonicalType> {
    match token {
        "text" => Some(CanonicalType::Text),
        "smallint" => Some(CanonicalType::SmallInt),
        "int" => Some(CanonicalType::Integer),
        "bigint" => Some(CanonicalType::BigInt),
        "uuid" => Some(match backend {
            BackendDialect::Sqlite => CanonicalType::Char(32),
            BackendDialect::Postgres => CanonicalType::Uuid,
        }),
        "timestamp" => Some(match backend {
            BackendDialect::Sqlite => CanonicalType::DateTime,
            BackendDialect::Postgres => CanonicalType::Timestamp,
        }),
        "timestamptz" => Some(match backend {
            BackendDialect::Sqlite => CanonicalType::DateTime,
            BackendDialect::Postgres => CanonicalType::TimestampTz,
        }),
        "date" => Some(CanonicalType::Date),
        "time" => Some(CanonicalType::Time),
        other => parse_varchar_token(other).map(|n| CanonicalType::Varchar(Some(n))),
    }
}

pub fn canonical_column_type_from_parts(
    db_type: Option<&str>,
    logical_type: Option<&str>,
    format: Option<&str>,
    backend: BackendDialect,
) -> CanonicalType {
    if let Some(token) = db_type
        && let Some(canonical) = db_type_token_to_canonical(token, backend)
    {
        return canonical;
    }
    match (logical_type, format) {
        (Some("string"), Some("date-time")) => CanonicalType::TimestampTz,
        (Some("string"), Some("date")) => CanonicalType::Date,
        (Some("string"), Some("uuid")) => CanonicalType::Uuid,
        (Some(_), Some("decimal")) => CanonicalType::Decimal,
        (Some("string"), Some("binary")) => CanonicalType::Blob,
        (Some(t), _) => json_type_to_canonical(t, backend),
        (None, _) => CanonicalType::Varchar(None),
    }
}

pub fn canonical_type_for_schema_column(
    column: &SchemaColumn,
    backend: BackendDialect,
) -> CanonicalType {
    canonical_column_type_from_parts(
        Some(column.db_type.as_str()),
        Some(column.logical_type.as_str()),
        column.format.as_deref(),
        backend,
    )
}

pub fn sqlite_declared_type(canonical: CanonicalType) -> String {
    match canonical {
        CanonicalType::Integer => "integer".to_string(),
        CanonicalType::SmallInt => "smallint".to_string(),
        CanonicalType::BigInt => "bigint".to_string(),
        CanonicalType::Double => "double".to_string(),
        CanonicalType::Decimal => "real".to_string(),
        CanonicalType::Boolean => "boolean".to_string(),
        CanonicalType::Json => "json_text".to_string(),
        CanonicalType::Text => "text".to_string(),
        CanonicalType::Varchar(None) => "varchar".to_string(),
        CanonicalType::Varchar(Some(n)) => format!("varchar({n})"),
        CanonicalType::Char(n) => format!("char({n})"),
        CanonicalType::Uuid => "uuid_text".to_string(),
        CanonicalType::DateTime | CanonicalType::Timestamp => "datetime_text".to_string(),
        CanonicalType::TimestampTz => "timestamp_with_timezone_text".to_string(),
        CanonicalType::Date => "date_text".to_string(),
        CanonicalType::Time => "time_text".to_string(),
        CanonicalType::Blob => "blob".to_string(),
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SqliteTypeClass {
    Integer,
    Text,
    Blob,
    Real,
    Numeric,
    Temporal,
}

pub fn sqlite_type_class(declared: &str) -> SqliteTypeClass {
    let declared = declared.to_ascii_lowercase();
    if declared.contains("date") || declared.contains("time") {
        return SqliteTypeClass::Temporal;
    }
    if declared.contains("json") {
        return SqliteTypeClass::Text;
    }
    if declared.contains("bool") || declared.contains("int") {
        return SqliteTypeClass::Integer;
    }
    if declared.contains("char") || declared.contains("clob") || declared.contains("text") {
        return SqliteTypeClass::Text;
    }
    if declared.is_empty() || declared.contains("blob") {
        return SqliteTypeClass::Blob;
    }
    if declared.contains("real")
        || declared.contains("floa")
        || declared.contains("doub")
        || declared.contains("num")
        || declared.contains("dec")
    {
        return SqliteTypeClass::Real;
    }
    SqliteTypeClass::Numeric
}

pub fn pg_alter_type_target(canonical: CanonicalType) -> String {
    match canonical {
        CanonicalType::Integer => "integer".to_string(),
        CanonicalType::SmallInt => "smallint".to_string(),
        CanonicalType::BigInt => "bigint".to_string(),
        CanonicalType::Double => "double precision".to_string(),
        CanonicalType::Decimal => "numeric".to_string(),
        CanonicalType::Boolean => "boolean".to_string(),
        CanonicalType::Json => "json".to_string(),
        CanonicalType::Text => "text".to_string(),
        CanonicalType::Varchar(None) => "varchar".to_string(),
        CanonicalType::Varchar(Some(n)) => format!("varchar({n})"),
        CanonicalType::Char(n) => format!("char({n})"),
        CanonicalType::Uuid => "uuid".to_string(),
        CanonicalType::DateTime | CanonicalType::Timestamp => "timestamp".to_string(),
        CanonicalType::TimestampTz => "timestamptz".to_string(),
        CanonicalType::Date => "date".to_string(),
        CanonicalType::Time => "time".to_string(),
        CanonicalType::Blob => "bytea".to_string(),
    }
}

pub fn literal_default_value(default: &serde_json::Value) -> Option<Value> {
    match default {
        serde_json::Value::Bool(value) => Some((*value).into()),
        serde_json::Value::Number(value) => value
            .as_i64()
            .map(Value::from)
            .or_else(|| value.as_f64().map(Value::from)),
        serde_json::Value::String(value) => Some(value.clone().into()),
        _ => None,
    }
}
