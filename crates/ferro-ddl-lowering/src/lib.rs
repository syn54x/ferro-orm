//! Canonical DDL lowering shared across Ferro emitters (AGENTS.md I-1).
//!
//! Type tokens, constraint naming, and column-definition helpers used by
//! `ferro-migrate` and (eventually) the runtime schema emitter.

use ferro_schema_ir::SchemaColumn;
use sea_query::{ColumnDef, ForeignKeyAction};

/// SQL dialect for lowering canonical types to rendered DDL.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Dialect {
    /// SQLite 3.
    Sqlite,
    /// PostgreSQL.
    Postgres,
}

/// Canonical, backend-resolved column type.
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

/// Apply a canonical type to a sea-query [`ColumnDef`].
pub fn apply_canonical_type(col_def: &mut ColumnDef, canonical: CanonicalType) {
    match canonical {
        CanonicalType::Integer => {
            col_def.integer();
        }
        CanonicalType::SmallInt => {
            col_def.small_integer();
        }
        CanonicalType::BigInt => {
            col_def.big_integer();
        }
        CanonicalType::Double => {
            col_def.double();
        }
        CanonicalType::Decimal => {
            col_def.decimal();
        }
        CanonicalType::Boolean => {
            col_def.boolean();
        }
        CanonicalType::Json => {
            col_def.json();
        }
        CanonicalType::Text => {
            col_def.text();
        }
        CanonicalType::Varchar(None) => {
            col_def.string();
        }
        CanonicalType::Varchar(Some(n)) => {
            col_def.string_len(n);
        }
        CanonicalType::Char(n) => {
            col_def.char_len(n);
        }
        CanonicalType::Uuid => {
            col_def.uuid();
        }
        CanonicalType::DateTime => {
            col_def.date_time();
        }
        CanonicalType::Timestamp => {
            col_def.timestamp();
        }
        CanonicalType::TimestampTz => {
            col_def.timestamp_with_time_zone();
        }
        CanonicalType::Date => {
            col_def.date();
        }
        CanonicalType::Time => {
            col_def.time();
        }
        CanonicalType::Blob => {
            col_def.blob();
        }
    }
}

fn parse_varchar_token(token: &str) -> Option<u32> {
    let body = token.strip_prefix("varchar(")?.strip_suffix(')')?;
    let n: u32 = body.parse().ok()?;
    if n == 0 { None } else { Some(n) }
}

/// Map a canonical `db_type` token to [`CanonicalType`].
pub fn db_type_token_to_canonical(token: &str, dialect: Dialect) -> Option<CanonicalType> {
    match token {
        "text" => Some(CanonicalType::Text),
        "smallint" => Some(CanonicalType::SmallInt),
        "int" => Some(CanonicalType::Integer),
        "bigint" => Some(CanonicalType::BigInt),
        "uuid" => Some(match dialect {
            Dialect::Sqlite => CanonicalType::Char(32),
            Dialect::Postgres => CanonicalType::Uuid,
        }),
        "timestamp" => Some(match dialect {
            Dialect::Sqlite => CanonicalType::DateTime,
            Dialect::Postgres => CanonicalType::Timestamp,
        }),
        "timestamptz" => Some(match dialect {
            Dialect::Sqlite => CanonicalType::DateTime,
            Dialect::Postgres => CanonicalType::TimestampTz,
        }),
        "date" => Some(CanonicalType::Date),
        "time" => Some(CanonicalType::Time),
        "varchar" => Some(CanonicalType::Varchar(None)),
        other => parse_varchar_token(other).map(|n| CanonicalType::Varchar(Some(n))),
    }
}

/// Resolve a [`SchemaColumn`] to its canonical storage type.
pub fn canonical_from_schema_column(
    col: &SchemaColumn,
    dialect: Dialect,
) -> Result<CanonicalType, String> {
    if let Some(canonical) = db_type_token_to_canonical(&col.db_type, dialect) {
        return Ok(canonical);
    }
    match (col.logical_type.as_str(), col.format.as_deref()) {
        ("string", Some("date-time")) => Ok(CanonicalType::TimestampTz),
        ("string", Some("date")) => Ok(CanonicalType::Date),
        ("string", Some("uuid")) => Ok(CanonicalType::Uuid),
        (_, Some("decimal")) => Ok(CanonicalType::Decimal),
        ("string", Some("binary")) => Ok(CanonicalType::Blob),
        ("integer", _) => Ok(CanonicalType::Integer),
        ("string", _) => Ok(CanonicalType::Varchar(None)),
        ("number", _) => Ok(CanonicalType::Double),
        ("boolean", _) => Ok(match dialect {
            Dialect::Sqlite => CanonicalType::Integer,
            Dialect::Postgres => CanonicalType::Boolean,
        }),
        ("object" | "array", _) => Ok(CanonicalType::Json),
        _ => Err(format!(
            "unknown db_type '{}' on column '{}'",
            col.db_type, col.name
        )),
    }
}

/// Single-column index name (`idx_<table>_<col>`).
pub fn single_index_name(table_lower: &str, col_name: &str) -> String {
    format!("idx_{table_lower}_{col_name}")
}

/// Single-column unique name with 63-char guard.
pub fn single_unique_index_name(table_lower: &str, col_name: &str) -> String {
    let raw = format!("uq_{table_lower}_{col_name}");
    if raw.chars().count() > 63 {
        return format!("{}_uq", raw.chars().take(60).collect::<String>());
    }
    raw
}

/// Composite index name (`idx_<table>_<cols>`).
pub fn composite_index_name(table_lower: &str, col_names: &[&str]) -> String {
    let joined = col_names.join("_");
    let raw = format!("idx_{table_lower}_{joined}");
    if raw.chars().count() > 63 {
        return format!("{}_idx", raw.chars().take(59).collect::<String>());
    }
    raw
}

/// Composite unique name (`uq_<table>_<cols>`).
pub fn composite_unique_index_name(table_lower: &str, col_names: &[&str]) -> String {
    let joined = col_names.join("_");
    let raw = format!("uq_{table_lower}_{joined}");
    if raw.chars().count() > 63 {
        return format!("{}_uq", raw.chars().take(60).collect::<String>());
    }
    raw
}

/// Check constraint name (`ck_<table>_<col>`).
pub fn db_check_constraint_name(table_lower: &str, col_name: &str) -> String {
    let raw = format!("ck_{table_lower}_{col_name}");
    if raw.chars().count() > 63 {
        return format!("{}_ck", raw.chars().take(60).collect::<String>());
    }
    raw
}

/// Postgres `ALTER COLUMN ... TYPE` target spelling.
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

/// SQLite declared-type string for a canonical type (parity-pinned).
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
pub(crate) enum SqliteTypeClass {
    Integer,
    Text,
    Blob,
    Real,
    Numeric,
    Temporal,
}

/// Storage-semantics class of a declared SQLite type.
pub(crate) fn sqlite_type_class(declared: &str) -> SqliteTypeClass {
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

/// Compare old and new SQLite declared types for storage-class drift.
pub fn sqlite_type_storage_drift(old_db_type: &str, new_canonical: CanonicalType) -> bool {
    let old_class = sqlite_type_class(old_db_type);
    let new_class = sqlite_type_class(&sqlite_declared_type(new_canonical));
    old_class != new_class
}

/// Quote a SQL identifier for Postgres/SQLite DDL.
pub fn quote_ident(ident: &str) -> String {
    format!("\"{}\"", ident.replace('"', "\"\""))
}

/// Map an `on_delete` action string to sea-query [`ForeignKeyAction`].
pub fn fk_action_from_str(on_delete: Option<&str>) -> ForeignKeyAction {
    match on_delete.unwrap_or("CASCADE").to_uppercase().as_str() {
        "RESTRICT" => ForeignKeyAction::Restrict,
        "SET NULL" => ForeignKeyAction::SetNull,
        "SET DEFAULT" => ForeignKeyAction::SetDefault,
        "NO ACTION" => ForeignKeyAction::NoAction,
        _ => ForeignKeyAction::Cascade,
    }
}

pub fn fk_action_sql(action: ForeignKeyAction) -> &'static str {
    match action {
        ForeignKeyAction::Restrict => "RESTRICT",
        ForeignKeyAction::SetNull => "SET NULL",
        ForeignKeyAction::SetDefault => "SET DEFAULT",
        ForeignKeyAction::NoAction => "NO ACTION",
        ForeignKeyAction::Cascade => "CASCADE",
    }
}

/// Convert a JSON-schema scalar default into a sea-query literal.
pub fn literal_default_value(default: &serde_json::Value) -> Option<sea_query::Value> {
    match default {
        serde_json::Value::Bool(value) => Some((*value).into()),
        serde_json::Value::Number(value) => value
            .as_i64()
            .map(sea_query::Value::from)
            .or_else(|| value.as_f64().map(sea_query::Value::from)),
        serde_json::Value::String(value) => Some(value.clone().into()),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn db_type_tokens_match_canonical_vocabulary() {
        assert_eq!(
            db_type_token_to_canonical("text", Dialect::Postgres),
            Some(CanonicalType::Text)
        );
        assert_eq!(
            db_type_token_to_canonical("varchar(40)", Dialect::Sqlite),
            Some(CanonicalType::Varchar(Some(40)))
        );
    }

    #[test]
    fn naming_helpers_match_i1_conventions() {
        assert_eq!(single_index_name("user", "email"), "idx_user_email");
        assert_eq!(single_unique_index_name("user", "email"), "uq_user_email");
        assert_eq!(db_check_constraint_name("user", "role"), "ck_user_role");
    }
}
