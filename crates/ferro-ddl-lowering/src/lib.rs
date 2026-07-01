//! Canonical DDL lowering shared across Ferro emitters (AGENTS.md I-1).
//!
//! Type tokens, constraint naming, and column-definition helpers used by
//! `ferro-migrate` and (eventually) the runtime schema emitter.

use ferro_schema_ir::SchemaColumn;
use sea_query::{ColumnDef, ForeignKeyAction};

/// The one SQL dialect / database backend Ferro targets. Selects both the
/// rendered SQL dialect and, in the runtime crate, the connection driver.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Dialect {
    /// SQLite 3.
    #[default]
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

fn parse_char_token(token: &str) -> Option<u32> {
    let body = token.strip_prefix("char(")?.strip_suffix(')')?;
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
        "boolean" => Some(match dialect {
            Dialect::Sqlite => CanonicalType::Integer,
            Dialect::Postgres => CanonicalType::Boolean,
        }),
        "double" => Some(CanonicalType::Double),
        "numeric" => Some(CanonicalType::Decimal),
        "json" => Some(CanonicalType::Json),
        "bytea" => Some(CanonicalType::Blob),
        "varchar" => Some(CanonicalType::Varchar(None)),
        other => parse_varchar_token(other)
            .map(|n| CanonicalType::Varchar(Some(n)))
            .or_else(|| parse_char_token(other).map(CanonicalType::Char)),
    }
}

/// Map a resolved [`CanonicalType`] back to the canonical Ferro `db_type` token
/// vocabulary used in SchemaIR and cross-emitter parity tests.
pub fn canonical_to_db_type_token(canonical: CanonicalType, dialect: Dialect) -> String {
    match canonical {
        CanonicalType::Integer => "int".to_string(),
        CanonicalType::SmallInt => "smallint".to_string(),
        CanonicalType::BigInt => "bigint".to_string(),
        CanonicalType::Double => "double".to_string(),
        CanonicalType::Decimal => "numeric".to_string(),
        CanonicalType::Boolean => "boolean".to_string(),
        CanonicalType::Json => "json".to_string(),
        CanonicalType::Text => "text".to_string(),
        CanonicalType::Varchar(None) => "varchar".to_string(),
        CanonicalType::Varchar(Some(n)) => format!("varchar({n})"),
        CanonicalType::Char(n) => match (dialect, n) {
            (Dialect::Sqlite, 32) => "uuid".to_string(),
            _ => format!("char({n})"),
        },
        CanonicalType::Uuid => "uuid".to_string(),
        CanonicalType::DateTime | CanonicalType::Timestamp => "timestamp".to_string(),
        CanonicalType::TimestampTz => "timestamptz".to_string(),
        CanonicalType::Date => "date".to_string(),
        CanonicalType::Time => "time".to_string(),
        CanonicalType::Blob => "bytea".to_string(),
    }
}

/// Map `information_schema.columns` spellings to the canonical Ferro `db_type` token
/// vocabulary (mirrors legacy `pg_type_matches` / sqlite storage classes).
pub fn information_schema_to_db_type_token(
    declared_type: &str,
    char_max_len: Option<i64>,
    dialect: Dialect,
) -> String {
    let lower = declared_type.to_ascii_lowercase();
    let base = match lower.as_str() {
        "boolean" => match dialect {
            Dialect::Sqlite => "int",
            Dialect::Postgres => "boolean",
        },
        "double precision" | "real" => "double",
        "numeric" => "numeric",
        "json" | "jsonb" => "json",
        "bytea" => "bytea",
        "text" => "text",
        "integer" => "int",
        "smallint" => "smallint",
        "bigint" => "bigint",
        "uuid" => "uuid",
        "date" => "date",
        "time without time zone" => "time",
        "timestamp without time zone" => "timestamp",
        "timestamp with time zone" => "timestamptz",
        _ if lower.contains("character varying") || lower == "varchar" => "varchar",
        _ if lower == "character" => "char",
        _ if lower.contains("smallint") => "smallint",
        _ if lower.contains("bigint") => "bigint",
        _ if lower.contains("int") => "int",
        _ if lower.contains("uuid") || lower.contains("char(32)") => "uuid",
        _ if lower.contains("timestamp with time zone") => "timestamptz",
        _ if lower.contains("timestamp") || lower.contains("datetime") => "timestamp",
        _ if lower == "date" || lower.contains("date_") => "date",
        _ if lower == "time" || lower.contains("time_") => "time",
        _ => "text",
    };
    match base {
        "varchar" => char_max_len
            .and_then(|n| u32::try_from(n).ok())
            .filter(|n| *n > 0)
            .map(|n| format!("varchar({n})"))
            .unwrap_or_else(|| "varchar".to_string()),
        "char" => char_max_len
            .and_then(|n| u32::try_from(n).ok())
            .filter(|n| *n > 0)
            .map(|n| format!("char({n})"))
            .unwrap_or_else(|| "char".to_string()),
        other => other.to_string(),
    }
}

/// Whether two [`SchemaColumn`] snapshots differ in resolved storage type.
pub fn schema_columns_storage_drift(
    old_col: &SchemaColumn,
    new_col: &SchemaColumn,
    dialect: Dialect,
) -> bool {
    match (
        canonical_from_schema_column(old_col, dialect),
        canonical_from_schema_column(new_col, dialect),
    ) {
        (Ok(old_c), Ok(new_c)) => {
            // Compare by storage token, not raw canonical: on SQLite both `Uuid`
            // and `Char(32)` map to "uuid" (and `DateTime`/`Timestamp` to
            // "timestamp"), so a derived model column does not read as drifted
            // against the token-round-tripped live column. Real changes
            // (int → bigint) still differ. (See #141.)
            canonical_to_db_type_token(old_c, dialect) != canonical_to_db_type_token(new_c, dialect)
        }
        // Reached only when canonical resolution fails for both columns. At runtime
        // both sides come from producers that always populate Some(...), so this
        // Option comparison is behavior-equivalent to the old String comparison.
        _ => old_col.db_type != new_col.db_type,
    }
}

/// Resolve a model property's `(logical_type, format, db_type)` to a canonical
/// type. A recognized `db_type` token wins; otherwise the logical-type + format
/// cascade decides. An empty/unrecognized `db_type` falls through.
///
/// Accepts both raw JSON Schema type values (`"string"` + format) **and** the
/// domain-specific `logical_type` tokens emitted by the Python SchemaIR compiler
/// (`"datetime"`, `"date"`, `"time"`, `"uuid"`, `"json"`, `"decimal"`), so that
/// compiled IR envelopes can be consumed directly by the migration planner.
pub fn canonical_from_parts(
    logical_type: &str,
    format: Option<&str>,
    db_type: &str,
    dialect: Dialect,
) -> Result<CanonicalType, String> {
    if let Some(canonical) = db_type_token_to_canonical(db_type, dialect) {
        return Ok(canonical);
    }
    match (logical_type, format) {
        // DEPRECATED (legacy JSON-Schema vocabulary): the Python SchemaIR compiler
        // now emits domain `logical_type` tokens ("datetime"/"date"/"uuid") instead
        // of "string"+format for these. These arms are now reached ONLY by
        // `plan_table_migration_legacy` (migrate.rs) via `build_column_plan` →
        // `canonical_column_type` → `canonical_from_parts`. The JSON create-table
        // emitter was removed in #153 (Phase 8.6). Removal of these arms is gated
        // on the Phase 9 legacy-planner removal (#108).
        // Note: "time" does NOT appear here because the legacy planner has no
        // ("string", "time") arm; it falls through to Varchar(None). See below.
        // Raw JSON Schema types with format — produced by schema_json_to_schema_ir.
        ("string", Some("date-time")) => Ok(CanonicalType::TimestampTz),
        ("string", Some("date")) => Ok(CanonicalType::Date),
        ("string", Some("uuid")) => Ok(CanonicalType::Uuid),
        // `format = "decimal"` is only ever emitted alongside a concrete logical
        // type (see src/ferro/schema_metadata.py), so `(None, Some("decimal"))`
        // never arises from a real model — this broad arm is safe.
        (_, Some("decimal")) => Ok(CanonicalType::Decimal),
        ("string", Some("binary")) => Ok(CanonicalType::Blob),
        // Domain-specific logical_type tokens emitted by the Python SchemaIR
        // compiler (compiler.py `_logical_type`). These are accepted alongside
        // the raw JSON Schema types so compiled IR envelopes can be consumed.
        // "datetime", "date", "uuid" have symmetric create-path counterparts.
        // Domain token for bytes/binary fields (compiler.py _logical_type emits "binary").
        ("binary", _) => Ok(CanonicalType::Blob),
        ("datetime", _) => Ok(CanonicalType::TimestampTz),
        ("date", _) => Ok(CanonicalType::Date),
        // "time" is the ASYMMETRIC case: schema.rs canonical_column_type has no
        // ("string", "time") arm, so datetime.time fields are created as varchar
        // on both SQLite and Postgres. Resolve to Varchar(None) here so the
        // consume side agrees with the live column token ("varchar") and no
        // spurious AlterColumnType / "use Alembic" warning fires. (#141 review.)
        ("time", _) => Ok(CanonicalType::Varchar(None)),
        ("uuid", _) => Ok(CanonicalType::Uuid),
        ("json", _) => Ok(CanonicalType::Json),
        ("decimal", _) => Ok(CanonicalType::Decimal),
        // Raw JSON Schema primitive types.
        ("integer", _) => Ok(CanonicalType::Integer),
        ("string", _) => Ok(CanonicalType::Varchar(None)),
        ("number", _) => Ok(CanonicalType::Double),
        ("boolean", _) => Ok(match dialect {
            Dialect::Sqlite => CanonicalType::Integer,
            Dialect::Postgres => CanonicalType::Boolean,
        }),
        ("object" | "array", _) => Ok(CanonicalType::Json),
        _ => Err(format!("unknown logical_type '{logical_type}'")),
    }
}

/// Resolve a [`SchemaColumn`] to its canonical storage type.
pub fn canonical_from_schema_column(
    col: &SchemaColumn,
    dialect: Dialect,
) -> Result<CanonicalType, String> {
    canonical_from_parts(&col.logical_type, col.format.as_deref(), col.db_type.as_deref().unwrap_or(""), dialect)
        .map_err(|reason| format!("unresolvable type on column '{}': {reason}", col.name))
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

/// The CHECK body for a `db_check` enum constraint — byte-identical across the
/// CREATE and ALTER emitters (and mirrored, escaping-free, by the Alembic emitter).
/// Quoting is double-quote on both backends; the wrapping `ALTER ... ADD CONSTRAINT`
/// is emitted only on Postgres (see `render_db_check`).
pub fn render_check_body(check: &ferro_schema_ir::SchemaCheck) -> String {
    format!("{} IN ({})", quote_ident(&check.column), check.values.join(", "))
}

/// The outcome of emitting a `db_check` constraint for one dialect.
#[derive(Debug)]
pub struct CheckEmission {
    /// The `ALTER TABLE ... ADD CONSTRAINT ... CHECK (...)` statement (Postgres only).
    pub statement: Option<String>,
    /// The SQLite elision warning (SQLite only).
    pub warning: Option<String>,
}

/// Single source for db_check emission: wrapper + dialect decision + body.
/// Postgres emits the ALTER; SQLite elides with a warning (no silent drop).
pub fn render_db_check(table: &str, check: &ferro_schema_ir::SchemaCheck, dialect: Dialect) -> CheckEmission {
    match dialect {
        Dialect::Postgres => CheckEmission {
            statement: Some(format!(
                "ALTER TABLE \"{table}\" ADD CONSTRAINT \"{}\" CHECK ({})",
                check.name,
                render_check_body(check),
            )),
            warning: None,
        },
        Dialect::Sqlite => CheckEmission {
            statement: None,
            warning: Some(format!(
                "Check constraint '{}' on table '{}' is not emitted on SQLite (requires table rebuild).",
                check.name, table
            )),
        },
    }
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

/// A `timestamp` ⇄ `timestamptz` change. Casting between them silently
/// reinterprets stored values under the session `TimeZone`, so auto-migrate
/// refuses it on Postgres (warn + skip) instead of executing it. `DateTime`
/// is the naive side (it lowers to the same `"timestamp"` token as `Timestamp`).
pub fn is_timestamp_tz_conversion(old: CanonicalType, new: CanonicalType) -> bool {
    use CanonicalType::*;
    matches!(
        (old, new),
        (Timestamp | DateTime, TimestampTz) | (TimestampTz, Timestamp | DateTime)
    )
}

/// The single-source warning for a refused `timestamp`⇄`timestamptz` auto-migrate
/// conversion on Postgres. Emitted identically by the IR emitter and the legacy
/// migrate planner so the shadow comparator sees matching plans.
pub fn timestamp_tz_conversion_warning(
    table: &str,
    column: &str,
    old_db_type: &str,
    new_target: &str,
    keep_db_type: &str,
) -> String {
    format!(
        "Column '{table}.{column}' is '{old_db_type}' in the database but the model maps \
         `datetime` to '{new_target}'. Ferro will not auto-convert it — a \
         timestamp/timestamptz cast reinterprets existing values under the \
         connection's timezone and can silently shift your data. To keep the column \
         as-is, annotate the field with db_type=\"{keep_db_type}\". To convert it \
         intentionally, use a reviewed migration (Alembic) with an explicit source \
         timezone."
    )
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

    #[test]
    fn information_schema_to_db_type_token_maps_live_spellings() {
        assert_eq!(
            information_schema_to_db_type_token("INTEGER", None, Dialect::Sqlite),
            "int"
        );
        assert_eq!(
            information_schema_to_db_type_token("DATETIME", None, Dialect::Sqlite),
            "timestamp"
        );
        assert_eq!(
            information_schema_to_db_type_token("character varying", Some(40), Dialect::Postgres),
            "varchar(40)"
        );
        assert_eq!(
            information_schema_to_db_type_token("jsonb", None, Dialect::Postgres),
            "json"
        );
        assert_eq!(
            information_schema_to_db_type_token("boolean", None, Dialect::Sqlite),
            "int"
        );
        assert_eq!(
            information_schema_to_db_type_token("boolean", None, Dialect::Postgres),
            "boolean"
        );
    }

    #[test]
    fn binary_logical_type_resolves_to_blob() {
        assert_eq!(
            canonical_from_parts("binary", None, "", Dialect::Sqlite),
            Ok(CanonicalType::Blob)
        );
        assert_eq!(
            canonical_from_parts("binary", None, "", Dialect::Postgres),
            Ok(CanonicalType::Blob)
        );
    }

    fn drift_col(name: &str, db_type: &str) -> SchemaColumn {
        SchemaColumn {
            name: name.to_string(),
            logical_type: "unknown".to_string(),
            db_type: Some(db_type.to_string()),
            db_type_explicit: None,
            nullable: true,
            primary_key: false,
            autoincrement: false,
            unique: false,
            index: false,
            default: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: false,
        }
    }

    /// Build a SchemaColumn for drift tests with explicit logical_type, format and db_type.
    fn col_with_db_type(
        name: &str,
        logical_type: &str,
        format: Option<&str>,
        db_type: Option<&str>,
    ) -> SchemaColumn {
        SchemaColumn {
            name: name.to_string(),
            logical_type: logical_type.to_string(),
            db_type: db_type.map(str::to_string),
            db_type_explicit: None,
            nullable: true,
            primary_key: false,
            autoincrement: false,
            unique: false,
            index: false,
            default: None,
            format: format.map(str::to_string),
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: false,
        }
    }

    #[test]
    fn uuid_model_does_not_drift_against_char32_live_on_sqlite() {
        // Live introspected `uuid_text` → db_type "uuid" → Char(32).
        let live = col_with_db_type("id", "string", Some("uuid"), Some("uuid"));
        // Model derived (post-#141 wire IR): db_type None, format "uuid" → Uuid.
        let model = col_with_db_type("id", "string", Some("uuid"), None);
        assert!(!schema_columns_storage_drift(&live, &model, Dialect::Sqlite));
    }

    /// Regression guard for the `"time"` phantom-drift false alarm (#141 review).
    ///
    /// The CREATE TABLE path (schema.rs `canonical_column_type`) has no
    /// `("string", "time")` arm: it falls through to `json_type_to_canonical("string")`
    /// → `Varchar(None)` → live column token `"varchar"`. The consume side must
    /// resolve `logical_type = "time"` to the same canonical so no spurious
    /// AlterColumnType fires on every startup for a `datetime.time` field.
    #[test]
    fn time_derived_does_not_drift_against_varchar_live_on_sqlite() {
        // Live column: created as varchar (what the CREATE path emits for time).
        let live = col_with_db_type("start_time", "string", None, Some("varchar"));
        // Model column: Python SchemaIR compiler emits logical_type="time", db_type=None.
        let model = col_with_db_type("start_time", "time", None, None);
        assert!(
            !schema_columns_storage_drift(&live, &model, Dialect::Sqlite),
            "time logical_type must resolve to Varchar(None) to match the live varchar column"
        );
    }

    #[test]
    fn time_derived_does_not_drift_against_varchar_live_on_postgres() {
        // Same parity check on Postgres: CREATE path also falls to Varchar(None)
        // for a time field (no ("string", "time") arm in schema.rs).
        let live = col_with_db_type("start_time", "string", None, Some("varchar"));
        let model = col_with_db_type("start_time", "time", None, None);
        assert!(
            !schema_columns_storage_drift(&live, &model, Dialect::Postgres),
            "time logical_type must resolve to Varchar(None) on Postgres as well"
        );
    }

    #[test]
    fn datetime_and_timestamp_are_storage_equivalent_on_sqlite() {
        // On SQLite, "timestamp" token → DateTime and "timestamptz" token → DateTime;
        // canonical_to_db_type_token maps DateTime → "timestamp" in both cases, so
        // the token comparison merges them. (On Postgres these give distinct canonicals.)
        let a = col_with_db_type("ts", "string", None, Some("timestamp"));
        let b = col_with_db_type("ts", "string", None, Some("timestamptz"));
        assert!(!schema_columns_storage_drift(&a, &b, Dialect::Sqlite));
        // Cross-check: same tokens DO differ on Postgres (Timestamp vs TimestampTz).
        assert!(schema_columns_storage_drift(&a, &b, Dialect::Postgres));
    }

    #[test]
    fn int_to_bigint_still_drifts() {
        let small = col_with_db_type("n", "integer", None, Some("int"));
        let big = col_with_db_type("n", "integer", None, Some("bigint"));
        assert!(schema_columns_storage_drift(&small, &big, Dialect::Sqlite));
    }

    #[test]
    fn schema_columns_storage_drift_compares_canonical_storage() {
        let old = drift_col("meta", "json");
        let new = drift_col("meta", "json");
        assert!(!schema_columns_storage_drift(&old, &new, Dialect::Postgres));

        let old = drift_col("meta", "jsonb");
        let new = drift_col("meta", "json");
        assert!(schema_columns_storage_drift(&old, &new, Dialect::Postgres));

        let old = drift_col("total", "numeric");
        let new = drift_col("total", "double");
        assert!(schema_columns_storage_drift(&old, &new, Dialect::Postgres));

        let old = drift_col("count", "varchar");
        let new = drift_col("count", "int");
        assert!(schema_columns_storage_drift(&old, &new, Dialect::Sqlite));
        assert!(schema_columns_storage_drift(&old, &new, Dialect::Postgres));

        let old = drift_col("created_at", "timestamp");
        let new = drift_col("created_at", "timestamptz");
        assert!(schema_columns_storage_drift(&old, &new, Dialect::Postgres));
    }

    #[test]
    fn canonical_to_db_type_token_roundtrips_core_tokens() {
        assert_eq!(canonical_to_db_type_token(CanonicalType::Integer, Dialect::Postgres), "int");
        assert_eq!(canonical_to_db_type_token(CanonicalType::Char(32), Dialect::Sqlite), "uuid");
        assert_eq!(canonical_to_db_type_token(CanonicalType::Char(10), Dialect::Sqlite), "char(10)");
        assert_eq!(canonical_to_db_type_token(CanonicalType::TimestampTz, Dialect::Postgres), "timestamptz");
        assert_eq!(canonical_to_db_type_token(CanonicalType::DateTime, Dialect::Postgres), "timestamp");
        assert_eq!(canonical_to_db_type_token(CanonicalType::Char(32), Dialect::Postgres), "char(32)");
    }

    #[test]
    fn canonical_from_parts_matches_schema_column_path() {
        // db_type wins
        assert_eq!(canonical_from_parts("string", None, "bigint", Dialect::Postgres), Ok(CanonicalType::BigInt));
        // fallback to logical_type/format
        assert_eq!(canonical_from_parts("string", Some("date-time"), "", Dialect::Postgres), Ok(CanonicalType::TimestampTz));
        assert_eq!(canonical_from_parts("integer", None, "", Dialect::Sqlite), Ok(CanonicalType::Integer));
        // unknown is an error (CREATE path maps this to Varchar at its call site)
        assert!(canonical_from_parts("mystery", None, "", Dialect::Postgres).is_err());
    }

    #[test]
    fn timestamp_tz_conversion_warning_names_column_db_type_and_alembic() {
        let w = timestamp_tz_conversion_warning(
            "event", "occurred_at", "timestamp", "timestamptz", "timestamp",
        );
        let col = w.find("event.occurred_at").expect("names the column");
        let dbt = w.find("db_type").expect("names db_type");
        let alembic = w.find("Alembic").expect("names Alembic");
        assert!(col < dbt && dbt < alembic, "tokens must appear in order: {w}");
    }

    #[test]
    fn is_timestamp_tz_conversion_matches_only_the_reinterpreting_pair() {
        use CanonicalType::*;
        // The reinterpreting pair, both directions (DateTime is the naive alias).
        assert!(is_timestamp_tz_conversion(Timestamp, TimestampTz));
        assert!(is_timestamp_tz_conversion(TimestampTz, Timestamp));
        assert!(is_timestamp_tz_conversion(DateTime, TimestampTz));
        assert!(is_timestamp_tz_conversion(TimestampTz, DateTime));
        // Not a tz reinterpretation.
        assert!(!is_timestamp_tz_conversion(Integer, BigInt));
        assert!(!is_timestamp_tz_conversion(Varchar(None), Integer));
        assert!(!is_timestamp_tz_conversion(Timestamp, Timestamp));
        assert!(!is_timestamp_tz_conversion(TimestampTz, TimestampTz));
        assert!(!is_timestamp_tz_conversion(Date, TimestampTz));
        assert!(!is_timestamp_tz_conversion(Timestamp, Date));
    }
}
