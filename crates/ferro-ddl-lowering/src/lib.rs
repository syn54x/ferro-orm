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
    /// Postgres JSONB. Never constructed for SQLite — the `jsonb` token
    /// lowers to [`CanonicalType::Json`] at the token→canonical seam
    /// (ADR-0004), like `boolean`→`Integer` and `uuid`→`Char(32)`.
    Jsonb,
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

/// Apply a canonical type to a sea-query [`ColumnDef`], dialect-aware.
///
/// On SQLite the temporal/uuid/json/decimal families use SQLAlchemy's declared
/// spellings (`DATETIME`, `DATE`, `TIME`, `CHAR(32)`, `JSON`, `NUMERIC`)
/// instead of sea-query's `*_text` defaults, so a Ferro-created database
/// reflects identically to an Alembic-created one (I-1; FF-B B5). SQLite's
/// type affinity makes the storage classes identical either way.
pub fn apply_canonical_type_for(
    col_def: &mut ColumnDef,
    canonical: CanonicalType,
    dialect: Dialect,
) {
    if dialect == Dialect::Sqlite {
        let sa_spelling = match canonical {
            CanonicalType::DateTime | CanonicalType::Timestamp | CanonicalType::TimestampTz => {
                Some("DATETIME")
            }
            CanonicalType::Date => Some("DATE"),
            CanonicalType::Time => Some("TIME"),
            CanonicalType::Uuid => Some("CHAR(32)"),
            // Jsonb is unreachable on SQLite (lowered at the token seam);
            // the arm keeps the match exhaustive and honest if it ever leaks.
            CanonicalType::Json | CanonicalType::Jsonb => Some("JSON"),
            CanonicalType::Decimal => Some("NUMERIC"),
            _ => None,
        };
        if let Some(spelling) = sa_spelling {
            col_def.custom(sea_query::Alias::new(spelling));
            return;
        }
    }
    apply_canonical_type(col_def, canonical);
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
        CanonicalType::Jsonb => {
            col_def.json_binary();
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
        "jsonb" => Some(match dialect {
            // Storage lowering (ADR-0004): SQLite stores jsonb-declared
            // columns as plain JSON; Jsonb never exists on SQLite.
            Dialect::Sqlite => CanonicalType::Json,
            Dialect::Postgres => CanonicalType::Jsonb,
        }),
        "bytea" => Some(CanonicalType::Blob),
        "blob" => Some(CanonicalType::Blob),
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
        CanonicalType::Jsonb => "jsonb".to_string(),
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
        "json" => "json",
        // Honest introspection (ADR-0004): live jsonb reads back as jsonb on
        // Postgres so declared-vs-live diffs are truthful. On SQLite the
        // jsonb token has no storage of its own (Storage lowering), so any
        // hand-created JSONB spelling normalizes to json.
        "jsonb" => match dialect {
            Dialect::Postgres => "jsonb",
            Dialect::Sqlite => "json",
        },
        "bytea" => "bytea",
        "blob" => "blob",
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
///
/// `old_col` is the live/introspected side, `new_col` the model side. When the
/// model resolves to a native Postgres enum, drift is decided by whether the
/// live column already IS a native enum (`postgres_native_enum` from
/// introspection) — a matching live enum is a no-op; a live scalar (the old
/// varchar lowering) is drift, which the emitters then REFUSE (warn + skip).
pub fn schema_columns_storage_drift(
    old_col: &SchemaColumn,
    new_col: &SchemaColumn,
    dialect: Dialect,
) -> bool {
    if let Ok(ResolvedStorage::PgEnum { .. }) = resolve_column_storage(new_col, dialect) {
        return !old_col.postgres_native_enum;
    }
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
        // Domain-specific logical_type tokens emitted by the Python SchemaIR
        // compiler (compiler.py `_logical_type`).
        (_, Some("decimal")) => Ok(CanonicalType::Decimal),
        ("binary", _) => Ok(CanonicalType::Blob),
        ("datetime", _) => Ok(CanonicalType::TimestampTz),
        ("date", _) => Ok(CanonicalType::Date),
        // FF-B B2: `datetime.time` stores as `time` on both dialects.
        ("time", _) => Ok(CanonicalType::Time),
        ("uuid", _) => Ok(CanonicalType::Uuid),
        // Default flip (ADR-0005): derived json-family storage is JSONB on
        // Postgres — the type Postgres itself recommends. Explicit
        // db_type="json" (handled above, token wins) is the opt-out for
        // key-order/byte fidelity. SQLite lowers to JSON either way.
        ("json", _) => Ok(match dialect {
            Dialect::Sqlite => CanonicalType::Json,
            Dialect::Postgres => CanonicalType::Jsonb,
        }),
        ("decimal", _) => Ok(CanonicalType::Decimal),
        // Raw JSON Schema primitive types (introspection / token round-trip).
        ("integer", _) => Ok(CanonicalType::Integer),
        ("string", _) => Ok(CanonicalType::Varchar(None)),
        ("number", _) => Ok(CanonicalType::Double),
        ("boolean", _) => Ok(match dialect {
            Dialect::Sqlite => CanonicalType::Integer,
            Dialect::Postgres => CanonicalType::Boolean,
        }),
        ("object" | "array", _) => Ok(match dialect {
            // Same default flip as the compiled `"json"` token above.
            Dialect::Sqlite => CanonicalType::Json,
            Dialect::Postgres => CanonicalType::Jsonb,
        }),
        _ => Err(format!("unknown logical_type '{logical_type}'")),
    }
}

/// Resolve a [`SchemaColumn`] to its canonical storage type.
pub fn canonical_from_schema_column(
    col: &SchemaColumn,
    dialect: Dialect,
) -> Result<CanonicalType, String> {
    canonical_from_parts(
        &col.logical_type,
        col.format.as_deref(),
        col.db_type.as_deref().unwrap_or(""),
        dialect,
    )
    .map_err(|reason| format!("unresolvable type on column '{}': {reason}", col.name))
}

/// Resolve a [`SchemaColumn`] to its logical codec type — the Python-facing
/// value family — without applying an explicit `db_type` override. Storage may
/// legally widen away from this family (e.g. UUID stored as `text`); the
/// logical canonical preserves bind/decode shapes.
pub fn logical_canonical_from_schema_column(
    col: &SchemaColumn,
    dialect: Dialect,
) -> Result<CanonicalType, String> {
    canonical_from_parts(&col.logical_type, col.format.as_deref(), "", dialect).map_err(|reason| {
        format!(
            "unresolvable logical type on column '{}': {reason}",
            col.name
        )
    })
}

/// A column's fully resolved storage: either a scalar [`CanonicalType`] or a
/// native Postgres enum type. This is THE derived-type decision table for
/// every emitter (FF-B B2) — the Alembic bridge consumes it mechanically over
/// FFI, so no Python code re-derives storage from `(logical_type, format)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ResolvedStorage {
    /// An ordinary scalar column type.
    Scalar(CanonicalType),
    /// A native Postgres enum: `CREATE TYPE <type_name> AS ENUM (<labels>)`,
    /// column typed as `<type_name>`. Postgres dialect only — on SQLite enum
    /// columns resolve to `Scalar(Varchar(max label length))`, matching what
    /// SQLAlchemy renders for `sa.Enum`.
    PgEnum {
        type_name: String,
        labels: Vec<String>,
    },
}

/// Stringify enum label values exactly as the Alembic bridge does (`str(v)` on
/// each member value): strings as-is, numbers via their decimal spelling,
/// booleans Python-style. Pinned by the int-enum bridge tests
/// (`test_standard_enum_generates_with_name`: labels `{"1", "2", "3"}`).
pub fn enum_label_strings(values: &[serde_json::Value]) -> Vec<String> {
    values
        .iter()
        .map(|v| match v {
            serde_json::Value::String(s) => s.clone(),
            serde_json::Value::Number(n) => n.to_string(),
            serde_json::Value::Bool(true) => "True".to_string(),
            serde_json::Value::Bool(false) => "False".to_string(),
            other => other.to_string(),
        })
        .collect()
}

/// Resolve a [`SchemaColumn`] to its storage decision. An EXPLICIT `db_type`
/// token wins (the user's override; `db_type_explicit` marks it); otherwise
/// `enum_values` selects native enum storage; otherwise the
/// [`canonical_from_parts`] cascade decides — where a non-explicit `db_type`
/// (live introspected columns; derived tokens) still resolves the scalar.
pub fn resolve_column_storage(
    col: &SchemaColumn,
    dialect: Dialect,
) -> Result<ResolvedStorage, String> {
    if col.db_type_explicit.unwrap_or(false)
        && let Some(canonical) =
            db_type_token_to_canonical(col.db_type.as_deref().unwrap_or(""), dialect)
    {
        return Ok(ResolvedStorage::Scalar(canonical));
    }
    if let Some(values) = col.enum_values.as_ref().filter(|v| !v.is_empty()) {
        let labels = enum_label_strings(values);
        return Ok(match dialect {
            Dialect::Postgres => ResolvedStorage::PgEnum {
                type_name: col
                    .enum_type_name
                    .clone()
                    .unwrap_or_else(|| col.name.clone()),
                labels,
            },
            Dialect::Sqlite => {
                let max_len = labels.iter().map(|l| l.chars().count()).max().unwrap_or(0);
                let max_len = u32::try_from(max_len).unwrap_or(u32::MAX);
                ResolvedStorage::Scalar(CanonicalType::Varchar(if max_len == 0 {
                    None
                } else {
                    Some(max_len)
                }))
            }
        });
    }
    canonical_from_schema_column(col, dialect).map(ResolvedStorage::Scalar)
}

/// The idempotent `CREATE TYPE ... AS ENUM` guard for a native Postgres enum.
/// Same DO-block pattern as [`render_db_check`]: it only *adds when absent*
/// (schema-scoped via `current_schema()`), so a second boot against an
/// already-migrated schema is a no-op. `DROP TYPE`/label cleanup is explicitly
/// out of scope — emission is additive; removals belong in reviewed migrations.
pub fn render_pg_enum_create_type(type_name: &str, labels: &[String]) -> String {
    let rendered_labels: Vec<String> = labels
        .iter()
        .map(|l| format!("'{}'", l.replace('\'', "''")))
        .collect();
    format!(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type t \
         JOIN pg_namespace n ON n.oid = t.typnamespace \
         WHERE t.typname = '{typname}' AND n.nspname = current_schema()) THEN \
         CREATE TYPE {quoted} AS ENUM ({labels}); \
         END IF; END $$",
        typname = type_name.replace('\'', "''"),
        quoted = quote_ident(type_name),
        labels = rendered_labels.join(", "),
    )
}

/// The label-addition decision (ADR-0011): which model-declared labels a live
/// ferro-owned enum type is missing, in declared order. This is the single
/// decision table for enum label drift — the auto-migrate planner and the
/// Alembic autogenerate comparator both consume it mechanically (AGENTS.md
/// § I-1); neither side re-derives it. Append-only by construction: labels the
/// live type has but the model lacks are not this function's concern.
pub fn missing_enum_labels(declared: &[String], live: &[String]) -> Vec<String> {
    declared
        .iter()
        .filter(|label| !live.contains(label))
        .cloned()
        .collect()
}

/// The warn-never-act half of the label-addition decision (ADR-0011): labels
/// the live type carries that the model no longer declares, in live (enum
/// sort) order. Rows may still hold these labels and older code may still be
/// running against the schema, so callers warn loudly and never remove —
/// removal and rename are reviewed-migration territory.
pub fn extra_enum_labels(declared: &[String], live: &[String]) -> Vec<String> {
    live.iter()
        .filter(|label| !declared.contains(label))
        .cloned()
        .collect()
}

/// The warn-never-act message for one drifted enum type, or `None` when
/// nothing is extra. Single-sourced like [`refused_conversion_warning`]:
/// callers emit it verbatim, never re-derive the wording.
pub fn extra_enum_labels_warning(type_name: &str, extra: &[String]) -> Option<String> {
    if extra.is_empty() {
        return None;
    }
    let listed: Vec<String> = extra.iter().map(|l| format!("'{l}'")).collect();
    Some(format!(
        "Enum type '{}' has label(s) {} that the model no longer declares. \
         Label addition is append-only: ferro never removes enum labels \
         (existing rows may still hold them). Remove or rename labels with \
         a reviewed Alembic migration.",
        type_name,
        listed.join(", ")
    ))
}

/// One `ALTER TYPE ... ADD VALUE IF NOT EXISTS` for a label addition.
/// `IF NOT EXISTS` makes concurrent boots and shared-type replans harmless;
/// executed outside transactions (autocommit) so the statement is legal on
/// every supported Postgres version and the label is committed before any
/// table plan that references it.
pub fn render_pg_enum_add_value(type_name: &str, label: &str) -> String {
    format!(
        "ALTER TYPE {} ADD VALUE IF NOT EXISTS '{}'",
        quote_ident(type_name),
        label.replace('\'', "''"),
    )
}

/// Detect a refused conversion from a live column to a resolved storage
/// target. Extends [`refused_scalar_conversion`] with the native-enum case:
/// a live non-enum (varchar/text) column targeted at a native Postgres enum
/// is refused; a live native-enum column is already at the target (no-op).
pub fn refused_conversion(
    old_col: &SchemaColumn,
    new_storage: &ResolvedStorage,
    dialect: Dialect,
) -> Option<RefusedConversion> {
    match new_storage {
        ResolvedStorage::PgEnum { .. } => {
            if old_col.postgres_native_enum {
                None
            } else {
                Some(RefusedConversion::VarcharToPgEnum)
            }
        }
        ResolvedStorage::Scalar(new_c) => {
            let old_c = canonical_from_schema_column(old_col, dialect).ok()?;
            refused_scalar_conversion(old_c, *new_c)
        }
    }
}

/// Single-column index name (`idx_<table>_<col>`) with 63-char guard.
pub fn single_index_name(table_lower: &str, col_name: &str) -> String {
    let raw = format!("idx_{table_lower}_{col_name}");
    if raw.chars().count() > 63 {
        return format!("{}_idx", raw.chars().take(59).collect::<String>());
    }
    raw
}

/// Foreign-key constraint name (`fk_<table>_<col>_<to_table>`) with 63-char guard.
/// Single source for both emitters (AGENTS.md § I-1); the IR compiler sets
/// `SchemaForeignKey.name` with this and both emitters render it.
pub fn fk_name(table_lower: &str, col_name: &str, to_table: &str) -> String {
    let raw = format!("fk_{table_lower}_{col_name}_{to_table}");
    if raw.chars().count() > 63 {
        return format!("{}_fk", raw.chars().take(60).collect::<String>());
    }
    raw
}

/// Whether a live constraint name follows the ferro FK convention above — the
/// ownership test: reconciliation only ever rebuilds names ferro emits;
/// constraints named any other way belong to the user and are never altered.
pub fn is_ferro_fk_name(name: &str) -> bool {
    name.starts_with("fk_")
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
    table_check_constraint_name(table_lower, col_name)
}

/// Table-check constraint name (`ck_<table>_<suffix>`) with 63-char guard.
pub fn table_check_constraint_name(table_lower: &str, suffix: &str) -> String {
    let raw = format!("ck_{table_lower}_{suffix}");
    if raw.chars().count() > 63 {
        return format!("{}_ck", raw.chars().take(60).collect::<String>());
    }
    raw
}

/// Render a structured check predicate to its CHECK body SQL fragment.
pub fn render_check_expr(expr: &ferro_schema_ir::CheckExpr) -> String {
    match expr {
        ferro_schema_ir::CheckExpr::And { left, right } => format!(
            "({}) AND ({})",
            render_check_expr(left),
            render_check_expr(right)
        ),
        ferro_schema_ir::CheckExpr::Or { left, right } => format!(
            "({}) OR ({})",
            render_check_expr(left),
            render_check_expr(right)
        ),
        ferro_schema_ir::CheckExpr::Not { child } => format!("NOT ({})", render_check_expr(child)),
        ferro_schema_ir::CheckExpr::IsNull { column } => {
            format!("{} IS NULL", quote_ident(column))
        }
        ferro_schema_ir::CheckExpr::IsNotNull { column } => {
            format!("{} IS NOT NULL", quote_ident(column))
        }
        ferro_schema_ir::CheckExpr::Cmp { column, op, other } => {
            let rhs = match other {
                ferro_schema_ir::CheckOperand::Column { name } => quote_ident(name),
                ferro_schema_ir::CheckOperand::Literal { token } => token.clone(),
            };
            format!(
                "{} {} {}",
                quote_ident(column),
                check_cmp_op_sql(*op),
                rhs
            )
        }
        ferro_schema_ir::CheckExpr::In { column, values } => {
            format!("{} IN ({})", quote_ident(column), values.join(", "))
        }
        ferro_schema_ir::CheckExpr::Like { column, pattern } => {
            format!("{} LIKE {}", quote_ident(column), pattern)
        }
    }
}

fn check_cmp_op_sql(op: ferro_schema_ir::CheckCmpOp) -> &'static str {
    match op {
        ferro_schema_ir::CheckCmpOp::Eq => "=",
        ferro_schema_ir::CheckCmpOp::Ne => "<>",
        ferro_schema_ir::CheckCmpOp::Lt => "<",
        ferro_schema_ir::CheckCmpOp::Le => "<=",
        ferro_schema_ir::CheckCmpOp::Gt => ">",
        ferro_schema_ir::CheckCmpOp::Ge => ">=",
    }
}

/// The CHECK body for a table check — byte-identical across emitters (I-1).
pub fn render_table_check_body(check: &ferro_schema_ir::SchemaTableCheck) -> String {
    render_check_expr(&check.predicate)
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
///
/// The Postgres emission is idempotent: the `ALTER TABLE ... ADD CONSTRAINT` is
/// guarded by a `DO $$ ... IF NOT EXISTS (pg_constraint) ...` block so a second
/// `connect(auto_migrate=True)` against an already-migrated schema is a no-op
/// rather than a hard `constraint "..." already exists` failure. Postgres has no
/// `ADD CONSTRAINT IF NOT EXISTS`, so the guard checks `pg_constraint` first — it
/// only *adds when absent* (it does not swallow an "already exists" error). The
/// CHECK body from [`render_check_body`] is embedded byte-identically inside the
/// guard, preserving the cross-emitter parity the Alembic mirror depends on.
pub fn render_db_check(table: &str, check: &ferro_schema_ir::SchemaCheck, dialect: Dialect) -> CheckEmission {
    match dialect {
        Dialect::Postgres => CheckEmission {
            statement: Some(format!(
                "DO $$ BEGIN \
                 IF NOT EXISTS (SELECT 1 FROM pg_constraint \
                 WHERE conname = '{conname}' AND conrelid = '\"{table}\"'::regclass) THEN \
                 ALTER TABLE \"{table}\" ADD CONSTRAINT \"{name}\" CHECK ({body}); \
                 END IF; END $$",
                conname = check.name.replace('\'', "''"),
                table = table,
                name = check.name,
                body = render_check_body(check),
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

/// The check-addition decision (ADR-0013): which declared CHECK constraints —
/// table checks and column checks alike — have no live constraint of that name,
/// in declared order (table checks, then column checks).
///
/// This is the single decision table for missing CHECKs: the auto-migrate
/// reconciliation pass consumes it through `ferro-migrate`, and the Alembic
/// autogenerate comparator consumes it over FFI (AGENTS.md § I-1). Neither side
/// re-derives it.
///
/// The comparison is by NAME only. A live constraint whose *body* drifted from
/// the declared predicate is a constraint rebuild, not an addition (ADR-0015,
/// #344), and a live name ferro does not own cannot collide with a declared
/// `ck_*` name — so a user-owned CHECK is never added over.
pub fn missing_check_names(
    model: &ferro_schema_ir::SchemaModel,
    live_names: &[String],
) -> Vec<String> {
    declared_check_names(model)
        .into_iter()
        .filter(|name| !live_names.iter().any(|live| live == name))
        .collect()
}

/// Every CHECK constraint name the model declares, table checks first.
fn declared_check_names(model: &ferro_schema_ir::SchemaModel) -> Vec<String> {
    model
        .table_checks
        .iter()
        .map(|check| check.name.clone())
        .chain(model.checks.iter().map(|check| check.name.clone()))
        .collect()
}

/// Render the ADD for one declared CHECK constraint, or `None` when `name` is
/// not declared on `model`.
///
/// The single source both migration doors consume for a missing CHECK
/// (AGENTS.md § I-1). A **table check** is one plain
/// `ALTER TABLE … ADD CONSTRAINT … CHECK (…)`: the caller only asks for it when
/// the name is absent live, so no existence guard is needed. A **column check**
/// reuses [`render_db_check`], the same idempotent DO-block the create path
/// emits. On SQLite both are skipped with a warning that names the constraint
/// (ADR-0014): adding a table constraint to an existing table needs a full
/// table rebuild, which is Alembic's batch-mode door.
pub fn render_check_addition(
    table: &str,
    model: &ferro_schema_ir::SchemaModel,
    name: &str,
    dialect: Dialect,
) -> Option<CheckEmission> {
    if let Some(check) = model.table_checks.iter().find(|check| check.name == name) {
        return Some(render_add_table_check(table, check, dialect));
    }
    let check = model.checks.iter().find(|check| check.name == name)?;
    Some(render_db_check(table, check, dialect))
}

/// The table-check half of [`render_check_addition`].
fn render_add_table_check(
    table: &str,
    check: &ferro_schema_ir::SchemaTableCheck,
    dialect: Dialect,
) -> CheckEmission {
    match dialect {
        Dialect::Postgres => CheckEmission {
            statement: Some(format!(
                "ALTER TABLE {} ADD CONSTRAINT {} CHECK ({})",
                quote_ident(table),
                quote_ident(&check.name),
                render_table_check_body(check),
            )),
            warning: None,
        },
        Dialect::Sqlite => CheckEmission {
            statement: None,
            warning: Some(format!(
                "Table check '{}' is declared on '{}' but missing from the live table, and \
                 SQLite cannot add a table constraint to an existing table (it requires a \
                 full table rebuild). The invariant is not database-enforced; use Alembic's \
                 batch mode to apply it.",
                check.name, table
            )),
        },
    }
}

/// Outcome of rendering a CHECK constraint rebuild (ADR-0015).
///
/// Postgres emits two statements — drop the live constraint, then a **bare**
/// `ALTER TABLE … ADD CONSTRAINT … CHECK (…)` so the new body actually
/// replaces the old one. The idempotent `render_db_check` DO-block is the
/// create/add path; using it here would no-op against the still-present name.
/// SQLite cannot alter constraints in place (ADR-0014).
#[derive(Debug)]
pub struct CheckRebuildEmission {
    /// DROP + ADD on Postgres; empty on SQLite.
    pub statements: Vec<String>,
    /// The SQLite skip warning (SQLite only).
    pub warning: Option<String>,
}

/// Canonicalize a CHECK definition so catalog wrapping, identifier quotes,
/// and whitespace are not drift (ADR-0015).
///
/// Both ferro's rendered CHECK body and a live catalog definition
/// (`pg_get_constraintdef`, SQLite's `CHECK (…)` fragment) pass through this
/// one function. A leading `CHECK` keyword is stripped; wrapping parentheses
/// that enclose the whole expression are unwrapped; simple identifiers are
/// compared unquoted. Postgres also paints `::type` casts onto literals and
/// may rewrite `IN (…)` as `= ANY (ARRAY[…])` — those are the same predicate.
pub fn normalize_check_definition(definition: &str) -> String {
    let tokens = unwrap_outer_parens(strip_pg_in_any(strip_type_casts(strip_leading_check(
        tokenize_check_sql(definition),
    ))));
    render_check_tokens(&tokens)
}

/// Declared CHECK names whose live counterpart exists and whose normalized
/// body differs from the model's canonical rendering.
///
/// Absent-live names are an add (`missing_check_names`, #343), not a rebuild.
/// Live names the model does not declare are leftover handling (#345).
pub fn drifted_check_names(
    model: &ferro_schema_ir::SchemaModel,
    live: &[(String, String)],
) -> Vec<String> {
    declared_check_names(model)
        .into_iter()
        .filter(|name| {
            let Some((_, live_def)) = live.iter().find(|(live_name, _)| live_name == name) else {
                return false;
            };
            let Some(canonical) = declared_check_body(model, name) else {
                return false;
            };
            normalize_check_definition(&canonical) != normalize_check_definition(live_def)
        })
        .collect()
}

/// Render the DROP+ADD (Postgres) or warn-skip (SQLite) for one declared
/// CHECK whose live body drifted. `None` when `name` is not on `model`.
pub fn render_check_rebuild(
    table: &str,
    model: &ferro_schema_ir::SchemaModel,
    name: &str,
    dialect: Dialect,
) -> Option<CheckRebuildEmission> {
    let body = declared_check_body(model, name)?;
    Some(match dialect {
        Dialect::Postgres => CheckRebuildEmission {
            statements: vec![
                format!(
                    "ALTER TABLE {} DROP CONSTRAINT {}",
                    quote_ident(table),
                    quote_ident(name),
                ),
                format!(
                    "ALTER TABLE {} ADD CONSTRAINT {} CHECK ({})",
                    quote_ident(table),
                    quote_ident(name),
                    body,
                ),
            ],
            warning: None,
        },
        Dialect::Sqlite => CheckRebuildEmission {
            statements: Vec::new(),
            warning: Some(format!(
                "CHECK constraint '{name}' on table '{table}' has a declared body that \
                 differs from the live constraint, and SQLite cannot alter constraints in \
                 place (it requires a full table rebuild). The live body remains; use \
                 Alembic's batch mode to apply the declared predicate."
            )),
        },
    })
}

fn declared_check_body(model: &ferro_schema_ir::SchemaModel, name: &str) -> Option<String> {
    if let Some(check) = model.table_checks.iter().find(|check| check.name == name) {
        return Some(render_table_check_body(check));
    }
    model
        .checks
        .iter()
        .find(|check| check.name == name)
        .map(render_check_body)
}

/// Live ferro-owned CHECK names the model no longer declares, in live order
/// (ADR-0013, #345).
///
/// Set-difference only: callers pass *already-filtered* ferro-owned names
/// (`LiveCheck.ferro_owned`, Alembic `ck_*`). This function does not inspect
/// the prefix — a user-owned name that leaked into `live_ferro_owned_names`
/// would be reported as extra. Missing names are an add
/// (`missing_check_names`, #343); same-name body drift is a rebuild
/// (`drifted_check_names`, #344).
pub fn extra_check_names(
    declared_names: &[String],
    live_ferro_owned_names: &[String],
) -> Vec<String> {
    live_ferro_owned_names
        .iter()
        .filter(|name| !declared_names.contains(name))
        .cloned()
        .collect()
}

/// The leftover-CHECK warning for one table, or `None` when nothing is extra.
/// Single-sourced like [`extra_enum_labels_warning`]: callers emit it verbatim,
/// never re-derive the wording. Names every leftover and points at
/// `migrate_destructive` / Alembic.
pub fn extra_check_names_warning(table: &str, extra: &[String]) -> Option<String> {
    if extra.is_empty() {
        return None;
    }
    let listed: Vec<String> = extra.iter().map(|name| format!("'{name}'")).collect();
    Some(format!(
        "Table '{table}' has CHECK constraint(s) {} that the model no longer \
         declares. Leftover CHECKs keep rejecting rows the model now allows. \
         They stay in place unless you pass migrate_destructive=True (Postgres) \
         or drop them with a reviewed Alembic migration.",
        listed.join(", "),
    ))
}

/// Render the DROP for one leftover CHECK constraint (ADR-0013, ADR-0014).
///
/// Postgres: one `ALTER TABLE … DROP CONSTRAINT`. SQLite: no statement, a
/// warning that names the constraint and points at Alembic batch mode — SQLite
/// cannot drop a table constraint without a full table rebuild.
pub fn render_check_drop(table: &str, name: &str, dialect: Dialect) -> CheckEmission {
    match dialect {
        Dialect::Postgres => CheckEmission {
            statement: Some(format!(
                "ALTER TABLE {} DROP CONSTRAINT {}",
                quote_ident(table),
                quote_ident(name),
            )),
            warning: None,
        },
        Dialect::Sqlite => CheckEmission {
            statement: None,
            warning: Some(format!(
                "CHECK constraint '{name}' on table '{table}' is no longer declared, \
                 and SQLite cannot drop a table constraint in place (it requires a \
                 full table rebuild). The live constraint remains; use Alembic's \
                 batch mode to drop it."
            )),
        },
    }
}

// ---------------------------------------------------------------------------
// Row security (PRD #406, #409): the single decision seam for `__ferro_rls__`.
// Every emitter — the runtime create pass today, the Alembic autogenerate
// operation (#414) and the reconciliation pass (#413) tomorrow — renders
// policies through these functions and never re-derives a name, a cast, or a
// statement (AGENTS.md § I-1).
// ---------------------------------------------------------------------------

/// Row policy name (`rls_<table>_<name>`) with the 63-char guard every other
/// ferro artifact name carries. `rls_` is the ferro-owned prefix for policies,
/// the way `ck_` is for checks and `fk_` is for foreign keys.
pub fn row_policy_name(table_lower: &str, name: &str) -> String {
    let raw = format!("rls_{table_lower}_{name}");
    if raw.chars().count() > 63 {
        return format!("{}_rls", raw.chars().take(59).collect::<String>());
    }
    raw
}

/// Whether a live policy name follows the ferro row-policy convention — the
/// ownership test reconciliation (#413) will use; policies named any other way
/// belong to the user and are never altered or dropped.
pub fn is_ferro_row_policy_name(name: &str) -> bool {
    name.starts_with("rls_")
}

/// The cast token the column/setting shorthand appends to
/// `NULLIF(current_setting(...), '')`, or `None` when the column already stores
/// text and no cast is needed.
///
/// The storage decision itself stays in [`resolve_column_storage`] (I-1); this
/// function only maps its Postgres result onto the shorthand allowlist —
/// `uuid`, `text`/`varchar`, and the integer families. Every other storage
/// (native enums, timestamps, numerics, json, …) is an `Err`: those comparisons
/// need an author's judgement about coercion, which is the raw `using=` form.
pub fn row_policy_shorthand_cast(
    col: &ferro_schema_ir::SchemaColumn,
) -> Result<Option<String>, String> {
    let storage = resolve_column_storage(col, Dialect::Postgres)?;
    let canonical = match storage {
        ResolvedStorage::Scalar(canonical) => canonical,
        ResolvedStorage::PgEnum { type_name, .. } => {
            return Err(format!(
                "column '{}' stores the native enum type '{}', which the RowPolicy \
                 column/setting shorthand does not support",
                col.name, type_name
            ));
        }
    };
    match canonical {
        CanonicalType::Uuid => Ok(Some("uuid".to_string())),
        CanonicalType::Text | CanonicalType::Varchar(_) => Ok(None),
        CanonicalType::SmallInt => Ok(Some("smallint".to_string())),
        CanonicalType::Integer => Ok(Some("integer".to_string())),
        CanonicalType::BigInt => Ok(Some("bigint".to_string())),
        other => Err(format!(
            "column '{}' stores '{}', which the RowPolicy column/setting shorthand \
             does not support",
            col.name,
            canonical_to_db_type_token(other, Dialect::Postgres)
        )),
    }
}

/// The shorthand's rendered comparison:
/// `"<col>" = NULLIF(current_setting('<key>', true), '')::<cast>`.
///
/// `NULLIF` is load-bearing. `current_setting(key, true)` returns NULL for a
/// setting that was never set, but a setting that was set and then `RESET`
/// reads back as the empty string — and `''::uuid` is an error, not a NULL. So
/// without the `NULLIF` a fail-closed policy would turn into a hard error on
/// every query for the whole recycled-connection case. With it, both shapes
/// evaluate to `col = NULL` → NULL → no rows.
pub fn render_row_policy_setting_expr(column: &str, setting: &str, cast: Option<&str>) -> String {
    let cast_suffix = match cast {
        Some(token) => format!("::{token}"),
        None => String::new(),
    };
    format!(
        "{} = NULLIF(current_setting('{}', true), ''){}",
        quote_ident(column),
        setting.replace('\'', "''"),
        cast_suffix,
    )
}

fn row_policy_command_sql(command: ferro_schema_ir::RowPolicyCommand) -> &'static str {
    match command {
        ferro_schema_ir::RowPolicyCommand::All => "ALL",
        ferro_schema_ir::RowPolicyCommand::Select => "SELECT",
        ferro_schema_ir::RowPolicyCommand::Insert => "INSERT",
        ferro_schema_ir::RowPolicyCommand::Update => "UPDATE",
        ferro_schema_ir::RowPolicyCommand::Delete => "DELETE",
    }
}

/// Whether Postgres accepts a `USING` clause for this command.
/// `FOR INSERT` policies are write-only: they take `WITH CHECK` alone.
pub fn row_policy_command_takes_using(command: ferro_schema_ir::RowPolicyCommand) -> bool {
    !matches!(command, ferro_schema_ir::RowPolicyCommand::Insert)
}

/// Whether Postgres accepts a `WITH CHECK` clause for this command.
/// `FOR SELECT` and `FOR DELETE` policies only read rows; they take `USING` alone.
pub fn row_policy_command_takes_with_check(command: ferro_schema_ir::RowPolicyCommand) -> bool {
    matches!(
        command,
        ferro_schema_ir::RowPolicyCommand::All
            | ferro_schema_ir::RowPolicyCommand::Insert
            | ferro_schema_ir::RowPolicyCommand::Update
    )
}

/// The `(USING, WITH CHECK)` expressions one policy renders, already filtered
/// to the clauses its command accepts.
///
/// The shorthand renders the SAME comparison into both clauses — reads and
/// writes are scoped by one declaration — so a `FOR ALL` shorthand policy both
/// hides other tenants' rows and rejects a write that would create one.
pub fn row_policy_clauses(
    model: &ferro_schema_ir::SchemaModel,
    policy: &ferro_schema_ir::SchemaRowPolicy,
) -> Result<(Option<String>, Option<String>), String> {
    let (using, with_check) = match &policy.expr {
        ferro_schema_ir::RowPolicyExpr::Setting { column, setting } => {
            let col = model
                .columns
                .iter()
                .find(|candidate| &candidate.name == column)
                .ok_or_else(|| {
                    format!(
                        "row policy '{}' references column '{}', which table '{}' does not have",
                        policy.name, column, model.table_name
                    )
                })?;
            let cast = row_policy_shorthand_cast(col)?;
            let rendered = render_row_policy_setting_expr(column, setting, cast.as_deref());
            (Some(rendered.clone()), Some(rendered))
        }
        ferro_schema_ir::RowPolicyExpr::Raw { using, with_check } => {
            (using.clone(), with_check.clone())
        }
    };
    Ok((
        using.filter(|_| row_policy_command_takes_using(policy.command)),
        with_check.filter(|_| row_policy_command_takes_with_check(policy.command)),
    ))
}

/// The full `CREATE POLICY` statement for one declared policy.
///
/// Shape: `CREATE POLICY "<name>" ON "<table>" [AS RESTRICTIVE] FOR <COMMAND>
/// [USING (...)] [WITH CHECK (...)]`. `AS PERMISSIVE` is Postgres's default and
/// stays implicit, so a permissive policy's catalog definition round-trips
/// without a phantom diff.
pub fn render_create_row_policy(
    model: &ferro_schema_ir::SchemaModel,
    policy: &ferro_schema_ir::SchemaRowPolicy,
) -> Result<String, String> {
    let (using, with_check) = row_policy_clauses(model, policy)?;
    let mut statement = format!(
        "CREATE POLICY {} ON {}",
        quote_ident(&policy.name),
        quote_ident(&model.table_name),
    );
    if policy.restrictive {
        statement.push_str(" AS RESTRICTIVE");
    }
    statement.push_str(&format!(" FOR {}", row_policy_command_sql(policy.command)));
    if let Some(expr) = using {
        statement.push_str(&format!(" USING ({expr})"));
    }
    if let Some(expr) = with_check {
        statement.push_str(&format!(" WITH CHECK ({expr})"));
    }
    Ok(statement)
}

/// `ALTER TABLE "<table>" ENABLE ROW LEVEL SECURITY`.
pub fn render_enable_row_security(table: &str) -> String {
    format!(
        "ALTER TABLE {} ENABLE ROW LEVEL SECURITY",
        quote_ident(table)
    )
}

/// `ALTER TABLE "<table>" FORCE ROW LEVEL SECURITY`.
pub fn render_force_row_security(table: &str) -> String {
    format!(
        "ALTER TABLE {} FORCE ROW LEVEL SECURITY",
        quote_ident(table)
    )
}

/// The row-security DDL for one freshly created table, or the SQLite skip.
#[derive(Debug, Default)]
pub struct RowSecurityEmission {
    /// ENABLE, FORCE (when declared), then one CREATE POLICY per policy.
    pub statements: Vec<String>,
    /// The SQLite skip warning — one per table, never one per policy.
    pub warning: Option<String>,
}

/// Every row-security statement a newly created table needs, in execution
/// order: `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY` when the
/// declaration asks for it, then one `CREATE POLICY` per declared policy in
/// declaration order.
///
/// Postgres-only (the glossary's "Postgres-only schema object" posture,
/// ADR-0014): on SQLite the model still registers and its table is still
/// created; the row-security DDL is skipped with one loud warning naming the
/// table, because SQLite has no row-level security to skip *to*.
pub fn row_security_statements(
    model: &ferro_schema_ir::SchemaModel,
    dialect: Dialect,
) -> Result<RowSecurityEmission, String> {
    let Some(declaration) = model.row_security.as_ref() else {
        return Ok(RowSecurityEmission::default());
    };
    if dialect == Dialect::Sqlite {
        return Ok(RowSecurityEmission {
            statements: Vec::new(),
            warning: Some(format!(
                "Table '{}' declares __ferro_rls__, but row-level security is a \
                 PostgreSQL-only feature: the table is created without its policies \
                 and rows are NOT filtered on SQLite. Run against PostgreSQL for \
                 enforcement.",
                model.table_name
            )),
        });
    }
    let mut statements = vec![render_enable_row_security(&model.table_name)];
    if declaration.force {
        statements.push(render_force_row_security(&model.table_name));
    }
    for policy in &declaration.policies {
        statements.push(render_create_row_policy(model, policy)?);
    }
    Ok(RowSecurityEmission {
        statements,
        warning: None,
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum CheckToken {
    Word(String),
    String(String),
    Punct(char),
}

fn tokenize_check_sql(input: &str) -> Vec<CheckToken> {
    let chars: Vec<char> = input.chars().collect();
    let mut tokens = Vec::new();
    let mut i = 0usize;
    while i < chars.len() {
        let ch = chars[i];
        if ch.is_whitespace() {
            i += 1;
            continue;
        }
        if ch == '\'' {
            let (lit, next) = take_quoted(&chars, i, '\'');
            tokens.push(CheckToken::String(lit));
            i = next;
            continue;
        }
        if ch == '"' {
            let (ident, next) = take_quoted(&chars, i, '"');
            let inner = ident[1..ident.len() - 1].replace("\"\"", "\"");
            if is_simple_ident(&inner) {
                tokens.push(CheckToken::Word(inner));
            } else {
                tokens.push(CheckToken::Word(ident));
            }
            i = next;
            continue;
        }
        if ch == ':' && i + 1 < chars.len() && chars[i + 1] == ':' {
            tokens.push(CheckToken::Punct(':'));
            tokens.push(CheckToken::Punct(':'));
            i += 2;
            continue;
        }
        if is_ident_start(ch) {
            let start = i;
            i += 1;
            while i < chars.len() && is_ident_continue(chars[i]) {
                i += 1;
            }
            tokens.push(CheckToken::Word(chars[start..i].iter().collect()));
            continue;
        }
        tokens.push(CheckToken::Punct(ch));
        i += 1;
    }
    tokens
}

fn take_quoted(chars: &[char], start: usize, quote: char) -> (String, usize) {
    let mut out = String::from(quote);
    let mut i = start + 1;
    while i < chars.len() {
        let ch = chars[i];
        out.push(ch);
        if ch == quote {
            if i + 1 < chars.len() && chars[i + 1] == quote {
                out.push(quote);
                i += 2;
                continue;
            }
            return (out, i + 1);
        }
        i += 1;
    }
    (out, i)
}

fn is_ident_start(ch: char) -> bool {
    ch.is_ascii_alphabetic() || ch == '_'
}

fn is_ident_continue(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || ch == '_'
}

fn is_simple_ident(s: &str) -> bool {
    let mut chars = s.chars();
    matches!(chars.next(), Some(ch) if is_ident_start(ch)) && chars.all(is_ident_continue)
}

fn strip_leading_check(mut tokens: Vec<CheckToken>) -> Vec<CheckToken> {
    if matches!(tokens.first(), Some(CheckToken::Word(w)) if w.eq_ignore_ascii_case("check")) {
        tokens.remove(0);
    }
    tokens
}

fn unwrap_outer_parens(mut tokens: Vec<CheckToken>) -> Vec<CheckToken> {
    loop {
        if tokens.len() < 2 {
            return tokens;
        }
        if tokens.first() != Some(&CheckToken::Punct('('))
            || tokens.last() != Some(&CheckToken::Punct(')'))
        {
            return tokens;
        }
        let mut depth = 0i32;
        let mut wraps_all = true;
        for (idx, token) in tokens.iter().enumerate() {
            match token {
                CheckToken::Punct('(') => depth += 1,
                CheckToken::Punct(')') => {
                    depth -= 1;
                    if depth == 0 && idx != tokens.len() - 1 {
                        wraps_all = false;
                        break;
                    }
                }
                _ => {}
            }
        }
        if !wraps_all || depth != 0 {
            return tokens;
        }
        tokens = tokens[1..tokens.len() - 1].to_vec();
    }
}

fn strip_type_casts(tokens: Vec<CheckToken>) -> Vec<CheckToken> {
    let mut out = Vec::with_capacity(tokens.len());
    let mut i = 0usize;
    while i < tokens.len() {
        if tokens[i] == CheckToken::Punct(':')
            && i + 1 < tokens.len()
            && tokens[i + 1] == CheckToken::Punct(':')
        {
            i += 2;
            if i < tokens.len() && matches!(tokens[i], CheckToken::Word(_)) {
                i += 1;
            }
            // varchar(n) / char(n)
            if i < tokens.len() && tokens[i] == CheckToken::Punct('(') {
                i += 1;
                while i < tokens.len() && tokens[i] != CheckToken::Punct(')') {
                    i += 1;
                }
                if i < tokens.len() {
                    i += 1;
                }
            }
            continue;
        }
        out.push(tokens[i].clone());
        i += 1;
    }
    out
}

fn strip_pg_in_any(tokens: Vec<CheckToken>) -> Vec<CheckToken> {
    // `col = ANY (ARRAY[a, b])` is how Postgres stores `col IN (a, b)`.
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < tokens.len() {
        let is_eq_any = tokens[i] == CheckToken::Punct('=')
            && i + 1 < tokens.len()
            && matches!(&tokens[i + 1], CheckToken::Word(w) if w.eq_ignore_ascii_case("any"));
        if is_eq_any {
            let mut j = i + 2;
            if j < tokens.len() && tokens[j] == CheckToken::Punct('(') {
                j += 1;
            }
            if j < tokens.len()
                && matches!(&tokens[j], CheckToken::Word(w) if w.eq_ignore_ascii_case("array"))
            {
                j += 1;
            }
            if j < tokens.len() && tokens[j] == CheckToken::Punct('[') {
                j += 1;
                let values_start = j;
                let mut depth = 1i32;
                while j < tokens.len() && depth > 0 {
                    match &tokens[j] {
                        CheckToken::Punct('[') => depth += 1,
                        CheckToken::Punct(']') => depth -= 1,
                        _ => {}
                    }
                    if depth > 0 {
                        j += 1;
                    }
                }
                let values_end = j;
                if j < tokens.len() && tokens[j] == CheckToken::Punct(']') {
                    j += 1;
                }
                if j < tokens.len() && tokens[j] == CheckToken::Punct(')') {
                    j += 1;
                }
                out.push(CheckToken::Word("IN".to_string()));
                out.push(CheckToken::Punct('('));
                out.extend(tokens[values_start..values_end].iter().cloned());
                out.push(CheckToken::Punct(')'));
                i = j;
                continue;
            }
        }
        out.push(tokens[i].clone());
        i += 1;
    }
    out
}

fn render_check_tokens(tokens: &[CheckToken]) -> String {
    let mut out = String::new();
    for (idx, token) in tokens.iter().enumerate() {
        let rendered = match token {
            CheckToken::Word(word) => canonicalize_sql_word(word),
            CheckToken::String(s) => s.clone(),
            CheckToken::Punct(ch) => ch.to_string(),
        };
        if idx > 0 && needs_space(&tokens[idx - 1], token) {
            out.push(' ');
        }
        out.push_str(&rendered);
    }
    out
}

fn canonicalize_sql_word(word: &str) -> String {
    const KEYWORDS: &[&str] = &[
        "AND", "OR", "NOT", "IS", "NULL", "IN", "LIKE", "ANY", "ARRAY", "CHECK", "TRUE", "FALSE",
    ];
    for keyword in KEYWORDS {
        if word.eq_ignore_ascii_case(keyword) {
            return (*keyword).to_string();
        }
    }
    word.to_string()
}

fn needs_space(prev: &CheckToken, next: &CheckToken) -> bool {
    match (prev, next) {
        (CheckToken::Punct('('), _) | (_, CheckToken::Punct(')')) => false,
        (_, CheckToken::Punct(',')) => false,
        (CheckToken::Punct(','), _) => true,
        (CheckToken::Punct(')'), CheckToken::Word(_)) => true,
        (CheckToken::Word(_), CheckToken::Punct('(')) => false,
        (CheckToken::Punct(_), CheckToken::Punct(_)) => false,
        _ => true,
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
        CanonicalType::Jsonb => "jsonb".to_string(),
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

/// A storage conversion auto-migrate refuses to execute (warn + skip, never a
/// silent ALTER). Each variant is a cast that can reinterpret or destroy stored
/// values, so it is left to a reviewed migration. `TimestampTz` is the original
/// #154 case; the other variants extend the same policy to the FF-B derived-type
/// changes (varchar-stored enums / times created by the old lowering).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RefusedConversion {
    /// `timestamp` ⇄ `timestamptz` (reinterprets under the session TimeZone).
    TimestampTz,
    /// live `varchar`/`text` → native Postgres enum type.
    VarcharToPgEnum,
    /// live `varchar`/`text` → `time` (old lowering stored `datetime.time` as varchar).
    VarcharToTime,
}

/// Detect a refused conversion between two scalar canonical storages.
/// (`VarcharToPgEnum` involves a non-scalar target and is detected where enum
/// resolution happens; it has no arm here.)
pub fn refused_scalar_conversion(
    old: CanonicalType,
    new: CanonicalType,
) -> Option<RefusedConversion> {
    use CanonicalType::*;
    if is_timestamp_tz_conversion(old, new) {
        return Some(RefusedConversion::TimestampTz);
    }
    if matches!((old, new), (Varchar(_) | Text, Time)) {
        return Some(RefusedConversion::VarcharToTime);
    }
    None
}

/// The single-source warning for any refused auto-migrate conversion. Emitted
/// identically by the IR emitter and the legacy migrate planner so the shadow
/// comparator sees matching plans. The `TimestampTz` arm returns the exact
/// legacy #154 text (already pinned by tests on both planner paths).
pub fn refused_conversion_warning(
    kind: RefusedConversion,
    table: &str,
    column: &str,
    old_db_type: &str,
    new_target: &str,
    keep_db_type: &str,
) -> String {
    match kind {
        RefusedConversion::TimestampTz => {
            timestamp_tz_conversion_warning(table, column, old_db_type, new_target, keep_db_type)
        }
        RefusedConversion::VarcharToPgEnum => format!(
            "Column '{table}.{column}' is '{old_db_type}' in the database but the model maps \
             this Enum field to the native Postgres enum type '{new_target}'. Ferro will not \
             auto-convert it — stored values outside the enum's labels would make the cast \
             fail mid-migration. To keep the column as-is, annotate the field with \
             db_type=\"{keep_db_type}\". To convert it intentionally, use a reviewed \
             migration (Alembic) that creates the type and casts with \
             USING \"{column}\"::\"{new_target}\"."
        ),
        RefusedConversion::VarcharToTime => format!(
            "Column '{table}.{column}' is '{old_db_type}' in the database but the model maps \
             `datetime.time` to '{new_target}'. Ferro will not auto-convert it — stored text \
             that does not parse as a time would make the cast fail mid-migration. To keep \
             the column as-is, annotate the field with db_type=\"{keep_db_type}\". To convert \
             it intentionally, use a reviewed migration (Alembic) with an explicit \
             USING cast."
        ),
    }
}

/// SQLite declared-type string for a canonical type (parity-pinned; matches
/// what [`apply_canonical_type_for`] renders on SQLite — SQLAlchemy-compatible
/// spellings since FF-B B5).
pub fn sqlite_declared_type(canonical: CanonicalType) -> String {
    match canonical {
        CanonicalType::Integer => "integer".to_string(),
        CanonicalType::SmallInt => "smallint".to_string(),
        CanonicalType::BigInt => "bigint".to_string(),
        CanonicalType::Double => "double".to_string(),
        CanonicalType::Decimal => "NUMERIC".to_string(),
        CanonicalType::Boolean => "boolean".to_string(),
        // Jsonb is unreachable on SQLite (lowered at the token seam); the arm
        // keeps the match exhaustive and matches apply_canonical_type_for.
        CanonicalType::Json | CanonicalType::Jsonb => "JSON".to_string(),
        CanonicalType::Text => "text".to_string(),
        CanonicalType::Varchar(None) => "varchar".to_string(),
        CanonicalType::Varchar(Some(n)) => format!("varchar({n})"),
        CanonicalType::Char(n) => format!("char({n})"),
        CanonicalType::Uuid => "CHAR(32)".to_string(),
        CanonicalType::DateTime | CanonicalType::Timestamp | CanonicalType::TimestampTz => {
            "DATETIME".to_string()
        }
        CanonicalType::Date => "DATE".to_string(),
        CanonicalType::Time => "TIME".to_string(),
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

/// DEFAULT right-hand side for a JSON object/array backfill, or [`None`] when
/// the value is not a container or the resolved storage is not json/jsonb.
///
/// Postgres spells `'<json>'::jsonb` or `'<json>'::json` from the storage
/// token. SQLite has no json/jsonb types: `'<json>'` with no cast. Object and
/// array on any other storage stay non-renderable (the add-column emitter
/// then falls through to [`literal_default_value`] and refuses).
pub fn render_json_backfill_default(
    default: &serde_json::Value,
    storage: &ResolvedStorage,
    dialect: Dialect,
) -> Option<String> {
    if !matches!(
        default,
        serde_json::Value::Object(_) | serde_json::Value::Array(_)
    ) {
        return None;
    }
    let json_token = match storage {
        ResolvedStorage::Scalar(CanonicalType::Json) => "json",
        ResolvedStorage::Scalar(CanonicalType::Jsonb) => "jsonb",
        _ => return None,
    };
    let body = default.to_string().replace('\'', "''");
    Some(match dialect {
        Dialect::Postgres => format!("'{body}'::{json_token}"),
        Dialect::Sqlite => format!("'{body}'"),
    })
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
        assert_eq!(
            table_check_constraint_name("transfer", "at_most_one_outflow"),
            "ck_transfer_at_most_one_outflow"
        );
    }

    #[test]
    fn table_check_constraint_name_truncates_above_63() {
        let long_suffix = "a".repeat(70);
        let result = table_check_constraint_name("verylongtable", &long_suffix);
        assert_eq!(result.chars().count(), 63);
        assert!(result.ends_with("_ck"));
        assert_eq!(
            result,
            db_check_constraint_name("verylongtable", &long_suffix),
            "table and column checks share the truncation rule"
        );
    }

    #[test]
    fn json_object_backfill_spells_storage_token() {
        let empty = serde_json::json!({});
        let jsonb = ResolvedStorage::Scalar(CanonicalType::Jsonb);
        let json = ResolvedStorage::Scalar(CanonicalType::Json);
        let text = ResolvedStorage::Scalar(CanonicalType::Text);
        assert_eq!(
            render_json_backfill_default(&empty, &jsonb, Dialect::Postgres).as_deref(),
            Some("'{}'::jsonb")
        );
        assert_eq!(
            render_json_backfill_default(&empty, &json, Dialect::Postgres).as_deref(),
            Some("'{}'::json")
        );
        assert_eq!(
            render_json_backfill_default(&empty, &json, Dialect::Sqlite).as_deref(),
            Some("'{}'")
        );
        assert_eq!(
            render_json_backfill_default(&empty, &text, Dialect::Postgres),
            None
        );
        assert_eq!(
            render_json_backfill_default(&serde_json::json!([]), &jsonb, Dialect::Postgres)
                .as_deref(),
            Some("'[]'::jsonb")
        );
        assert_eq!(
            render_json_backfill_default(&serde_json::json!("draft"), &jsonb, Dialect::Postgres),
            None
        );
    }

    fn transfer_at_most_one_outflow_check() -> ferro_schema_ir::SchemaTableCheck {
        use ferro_schema_ir::CheckExpr;
        ferro_schema_ir::SchemaTableCheck {
            name: "ck_transfer_at_most_one_outflow".to_string(),
            predicate: CheckExpr::Or {
                left: Box::new(CheckExpr::IsNull {
                    column: "outflow_transaction_id".to_string(),
                }),
                right: Box::new(CheckExpr::IsNull {
                    column: "outflow_activity_id".to_string(),
                }),
            },
        }
    }

    #[test]
    fn render_table_check_body_pins_transfer_is_null_or() {
        assert_eq!(
            render_table_check_body(&transfer_at_most_one_outflow_check()),
            "(\"outflow_transaction_id\" IS NULL) OR (\"outflow_activity_id\" IS NULL)"
        );
    }

    fn transfer_model_with_checks(
        table_checks: Vec<ferro_schema_ir::SchemaTableCheck>,
        checks: Vec<ferro_schema_ir::SchemaCheck>,
    ) -> ferro_schema_ir::SchemaModel {
        ferro_schema_ir::SchemaModel {
            model_name: "transfer".to_string(),
            table_name: "transfer".to_string(),
            columns: Vec::new(),
            foreign_keys: Vec::new(),
            indexes: Vec::new(),
            uniques: Vec::new(),
            checks,
            table_checks,
            row_security: None,
        }
    }

    fn account_role_column_check() -> ferro_schema_ir::SchemaCheck {
        ferro_schema_ir::SchemaCheck {
            name: "ck_transfer_kind".to_string(),
            column: "kind".to_string(),
            values: vec!["'in'".to_string(), "'out'".to_string()],
        }
    }

    #[test]
    fn missing_check_names_reports_declared_names_absent_live() {
        let model = transfer_model_with_checks(
            vec![transfer_at_most_one_outflow_check()],
            vec![account_role_column_check()],
        );
        assert_eq!(
            missing_check_names(&model, &["ck_transfer_kind".to_string()]),
            vec!["ck_transfer_at_most_one_outflow".to_string()],
            "table checks come first, and a live name is never re-added"
        );
        assert!(
            missing_check_names(
                &model,
                &[
                    "ck_transfer_at_most_one_outflow".to_string(),
                    "ck_transfer_kind".to_string(),
                ],
            )
            .is_empty(),
            "a reconciled table replans to nothing"
        );
    }

    #[test]
    fn missing_check_names_ignores_user_owned_live_constraints() {
        let model = transfer_model_with_checks(vec![transfer_at_most_one_outflow_check()], vec![]);
        assert_eq!(
            missing_check_names(&model, &["transfer_positive_amount".to_string()]),
            vec!["ck_transfer_at_most_one_outflow".to_string()],
            "a user-owned live CHECK is not a counterpart for a declared ck_* name"
        );
    }

    #[test]
    fn render_check_addition_table_check_is_one_plain_alter_on_postgres() {
        let model = transfer_model_with_checks(vec![transfer_at_most_one_outflow_check()], vec![]);
        let emission = render_check_addition(
            "transfer",
            &model,
            "ck_transfer_at_most_one_outflow",
            Dialect::Postgres,
        )
        .expect("declared table check must resolve");
        assert_eq!(
            emission.statement.as_deref(),
            Some(
                "ALTER TABLE \"transfer\" ADD CONSTRAINT \"ck_transfer_at_most_one_outflow\" \
                 CHECK ((\"outflow_transaction_id\" IS NULL) OR \
                 (\"outflow_activity_id\" IS NULL))"
            )
        );
        assert!(emission.warning.is_none());
    }

    #[test]
    fn render_check_addition_table_check_warns_and_skips_on_sqlite() {
        let model = transfer_model_with_checks(vec![transfer_at_most_one_outflow_check()], vec![]);
        let emission = render_check_addition(
            "transfer",
            &model,
            "ck_transfer_at_most_one_outflow",
            Dialect::Sqlite,
        )
        .expect("declared table check must resolve");
        assert!(emission.statement.is_none(), "ADR-0014: no SQLite ALTER");
        let warning = emission.warning.expect("SQLite must never skip silently");
        assert!(
            warning.contains("ck_transfer_at_most_one_outflow"),
            "{warning}"
        );
        assert!(warning.contains("Alembic"), "{warning}");
    }

    #[test]
    fn render_check_addition_column_check_reuses_the_idempotent_do_block() {
        let check = account_role_column_check();
        let model = transfer_model_with_checks(vec![], vec![check.clone()]);
        let emission =
            render_check_addition("transfer", &model, "ck_transfer_kind", Dialect::Postgres)
                .expect("declared column check must resolve");
        assert_eq!(
            emission.statement,
            render_db_check("transfer", &check, Dialect::Postgres).statement,
            "the column-check ADD is single-sourced with the create path"
        );
    }

    #[test]
    fn render_check_addition_returns_none_for_an_undeclared_name() {
        let model = transfer_model_with_checks(vec![transfer_at_most_one_outflow_check()], vec![]);
        assert!(
            render_check_addition("transfer", &model, "ck_transfer_nope", Dialect::Postgres)
                .is_none()
        );
    }

    // -----------------------------------------------------------------------
    // Check-body drift (#344; ADR-0015): same live name, different body is a
    // rebuild. Catalog wrapping / quoting / whitespace is not drift.
    // -----------------------------------------------------------------------

    #[test]
    fn catalog_shaped_transfer_check_normalizes_equal_to_rendered_body() {
        let canonical = render_table_check_body(&transfer_at_most_one_outflow_check());
        // pg_get_constraintdef for the Transfer at-most-one-outflow shape:
        // CHECK prefix, extra wrapping parens, unquoted idents.
        let catalog = "CHECK (((outflow_transaction_id IS NULL) OR (outflow_activity_id IS NULL)))";
        assert_eq!(
            normalize_check_definition(catalog),
            normalize_check_definition(&canonical),
            "catalog noise must not look like a predicate change"
        );
        assert_ne!(
            catalog, canonical,
            "the pin is only meaningful if the raw strings actually differ"
        );
    }

    #[test]
    fn whitespace_and_quoting_only_is_not_drift() {
        let model = transfer_model_with_checks(vec![transfer_at_most_one_outflow_check()], vec![]);
        let live = [(
            "ck_transfer_at_most_one_outflow".to_string(),
            "CHECK ( ( \"outflow_transaction_id\" IS NULL ) OR ( \"outflow_activity_id\" IS NULL ) )"
                .to_string(),
        )];
        assert!(
            drifted_check_names(&model, &live).is_empty(),
            "quoting and whitespace are catalog noise, not a rebuild"
        );
    }

    #[test]
    fn a_real_predicate_change_is_drift() {
        let model = transfer_model_with_checks(vec![transfer_at_most_one_outflow_check()], vec![]);
        let live = [(
            "ck_transfer_at_most_one_outflow".to_string(),
            "CHECK ((\"outflow_transaction_id\" IS NULL) AND (\"outflow_activity_id\" IS NULL))"
                .to_string(),
        )];
        assert_eq!(
            drifted_check_names(&model, &live),
            vec!["ck_transfer_at_most_one_outflow".to_string()]
        );
    }

    #[test]
    fn drifted_check_names_skips_absent_live_and_undeclared_live() {
        let model = transfer_model_with_checks(vec![transfer_at_most_one_outflow_check()], vec![]);
        assert!(
            drifted_check_names(&model, &[]).is_empty(),
            "a missing name is an add (#343), not a rebuild"
        );
        let leftover = [("ck_transfer_orphan".to_string(), "CHECK (true)".to_string())];
        assert!(
            drifted_check_names(&model, &leftover).is_empty(),
            "an undeclared live name is leftover handling (#345), not a rebuild"
        );
    }

    #[test]
    #[test]
    fn postgres_in_any_array_and_text_casts_are_not_drift() {
        let check = account_role_column_check();
        let canonical = render_check_body(&check);
        let catalog = "CHECK ((kind = ANY (ARRAY['in'::text, 'out'::text])))";
        assert_eq!(
            normalize_check_definition(catalog),
            normalize_check_definition(&canonical),
        );
    }

    #[test]
    fn column_check_label_change_is_drift() {
        let check = account_role_column_check();
        let model = transfer_model_with_checks(vec![], vec![check]);
        let live = [(
            "ck_transfer_kind".to_string(),
            "CHECK (\"kind\" IN ('in'))".to_string(),
        )];
        assert_eq!(
            drifted_check_names(&model, &live),
            vec!["ck_transfer_kind".to_string()]
        );
    }

    #[test]
    fn render_check_rebuild_is_drop_then_bare_add_on_postgres() {
        let model = transfer_model_with_checks(vec![transfer_at_most_one_outflow_check()], vec![]);
        let emission = render_check_rebuild(
            "transfer",
            &model,
            "ck_transfer_at_most_one_outflow",
            Dialect::Postgres,
        )
        .expect("declared table check must resolve");
        assert_eq!(
            emission.statements,
            vec![
                "ALTER TABLE \"transfer\" DROP CONSTRAINT \"ck_transfer_at_most_one_outflow\""
                    .to_string(),
                "ALTER TABLE \"transfer\" ADD CONSTRAINT \"ck_transfer_at_most_one_outflow\" \
                 CHECK ((\"outflow_transaction_id\" IS NULL) OR \
                 (\"outflow_activity_id\" IS NULL))"
                    .to_string(),
            ]
        );
        assert!(emission.warning.is_none());
        assert!(
            !emission.statements.iter().any(|sql| sql.contains("DO $$")),
            "a rebuild must replace the body; the idempotent DO-block would no-op"
        );
    }

    #[test]
    fn render_check_rebuild_column_check_is_also_a_bare_add() {
        let check = account_role_column_check();
        let model = transfer_model_with_checks(vec![], vec![check]);
        let emission =
            render_check_rebuild("transfer", &model, "ck_transfer_kind", Dialect::Postgres)
                .expect("declared column check must resolve");
        assert_eq!(
            emission.statements,
            vec![
                "ALTER TABLE \"transfer\" DROP CONSTRAINT \"ck_transfer_kind\"".to_string(),
                "ALTER TABLE \"transfer\" ADD CONSTRAINT \"ck_transfer_kind\" \
                 CHECK (\"kind\" IN ('in', 'out'))"
                    .to_string(),
            ]
        );
    }

    #[test]
    fn render_check_rebuild_warns_and_skips_on_sqlite() {
        let model = transfer_model_with_checks(vec![transfer_at_most_one_outflow_check()], vec![]);
        let emission = render_check_rebuild(
            "transfer",
            &model,
            "ck_transfer_at_most_one_outflow",
            Dialect::Sqlite,
        )
        .expect("declared table check must resolve");
        assert!(emission.statements.is_empty(), "ADR-0014: no SQLite ALTER");
        let warning = emission.warning.expect("SQLite must never skip silently");
        assert!(
            warning.contains("ck_transfer_at_most_one_outflow"),
            "{warning}"
        );
        assert!(warning.contains("Alembic"), "{warning}");
    }

    #[test]
    fn render_check_rebuild_returns_none_for_an_undeclared_name() {
        let model = transfer_model_with_checks(vec![transfer_at_most_one_outflow_check()], vec![]);
        assert!(
            render_check_rebuild("transfer", &model, "ck_transfer_nope", Dialect::Postgres)
                .is_none()
        );
    }

    // Leftover ferro-owned CHECKs (#345; ADR-0013): live name not in the
    // declared set. extra_check_names is a set-difference only — the
    // ferro_owned / ck_* filter is the caller's (migrate.rs / Alembic).

    #[test]
    fn extra_check_names_returns_undeclared_live_names_in_live_order() {
        let declared = vec![
            "ck_transfer_at_most_one_outflow".to_string(),
            "ck_transfer_kind".to_string(),
        ];
        let live = vec![
            "ck_transfer_orphan".to_string(),
            "ck_transfer_kind".to_string(),
            "ck_transfer_old".to_string(),
        ];
        assert_eq!(
            extra_check_names(&declared, &live),
            vec!["ck_transfer_orphan", "ck_transfer_old"]
        );
        assert!(extra_check_names(&declared, &declared).is_empty());
    }

    #[test]
    fn extra_check_names_is_a_set_difference_only() {
        let declared = vec!["ck_transfer_kind".to_string()];
        let live = vec![
            "ck_transfer_kind".to_string(),
            "transfer_positive_amount".to_string(),
        ];
        assert_eq!(
            extra_check_names(&declared, &live),
            vec!["transfer_positive_amount"],
            "prefix filtering is the caller's; this function does not drop non-ck_* names"
        );
        assert!(extra_check_names(&declared, &[]).is_empty());
    }

    #[test]
    fn extra_check_names_warning_is_pinned_and_names_every_leftover() {
        assert_eq!(
            extra_check_names_warning(
                "transfer",
                &[
                    "ck_transfer_orphan".to_string(),
                    "ck_transfer_kind".to_string(),
                ],
            ),
            Some(
                "Table 'transfer' has CHECK constraint(s) 'ck_transfer_orphan', \
                 'ck_transfer_kind' that the model no longer declares. Leftover \
                 CHECKs keep rejecting rows the model now allows. They stay in \
                 place unless you pass migrate_destructive=True (Postgres) or \
                 drop them with a reviewed Alembic migration."
                    .to_string()
            )
        );
        assert_eq!(extra_check_names_warning("transfer", &[]), None);
    }

    #[test]
    fn render_check_drop_is_one_alter_on_postgres() {
        let emission = render_check_drop("transfer", "ck_transfer_orphan", Dialect::Postgres);
        assert_eq!(
            emission.statement.as_deref(),
            Some(r#"ALTER TABLE "transfer" DROP CONSTRAINT "ck_transfer_orphan""#)
        );
        assert!(emission.warning.is_none());
    }

    #[test]
    fn render_check_drop_warns_and_skips_on_sqlite() {
        let emission = render_check_drop("transfer", "ck_transfer_orphan", Dialect::Sqlite);
        assert!(emission.statement.is_none(), "ADR-0014: no SQLite ALTER");
        let warning = emission.warning.expect("SQLite must never skip silently");
        assert!(warning.contains("ck_transfer_orphan"), "{warning}");
        assert!(warning.contains("Alembic"), "{warning}");
        assert!(warning.contains("batch"), "{warning}");
    }

    #[test]
    fn render_check_expr_covers_cmp_in_and_like() {
        use ferro_schema_ir::{CheckCmpOp, CheckExpr, CheckOperand};
        assert_eq!(
            render_check_expr(&CheckExpr::Cmp {
                column: "amount".to_string(),
                op: CheckCmpOp::Ge,
                other: CheckOperand::Literal {
                    token: "0".to_string(),
                },
            }),
            "\"amount\" >= 0"
        );
        assert_eq!(
            render_check_expr(&CheckExpr::In {
                column: "status".to_string(),
                values: vec!["'draft'".to_string(), "'active'".to_string()],
            }),
            "\"status\" IN ('draft', 'active')"
        );
        assert_eq!(
            render_check_expr(&CheckExpr::Like {
                column: "code".to_string(),
                pattern: "'A%'".to_string(),
            }),
            "\"code\" LIKE 'A%'"
        );
        assert_eq!(
            render_check_expr(&CheckExpr::Not {
                child: Box::new(CheckExpr::IsNotNull {
                    column: "deleted_at".to_string(),
                }),
            }),
            "NOT (\"deleted_at\" IS NOT NULL)"
        );
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
        // Intentional flip (#263, ADR-0004): live jsonb no longer collapses
        // to json on Postgres — introspection is honest per storage token.
        assert_eq!(
            information_schema_to_db_type_token("jsonb", None, Dialect::Postgres),
            "jsonb"
        );
        assert_eq!(
            information_schema_to_db_type_token("json", None, Dialect::Postgres),
            "json"
        );
        // SQLite has no jsonb storage of its own — lowering normalizes it.
        assert_eq!(
            information_schema_to_db_type_token("jsonb", None, Dialect::Sqlite),
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
        assert_eq!(
            information_schema_to_db_type_token("BLOB", None, Dialect::Sqlite),
            "blob"
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
        // Model derived IR: logical_type "uuid" (Python SchemaIR compiler).
        let model = col_with_db_type("id", "uuid", None, None);
        assert!(!schema_columns_storage_drift(&live, &model, Dialect::Sqlite));
    }

    /// Since FF-B B2, `datetime.time` fields store as `time` on both dialects
    /// (resolving the #141 asymmetry the other way): a live varchar column
    /// from the old lowering now reads as drift — and the conversion is
    /// REFUSED at emission (`RefusedConversion::VarcharToTime`), never a
    /// silent ALTER. A live `time` column does not drift.
    #[test]
    fn time_model_drifts_against_legacy_varchar_live_and_matches_time_live() {
        let live_varchar = col_with_db_type("start_time", "string", None, Some("varchar"));
        let model = col_with_db_type("start_time", "time", None, None);
        for dialect in [Dialect::Sqlite, Dialect::Postgres] {
            assert!(
                schema_columns_storage_drift(&live_varchar, &model, dialect),
                "legacy varchar-stored time must surface as drift ({dialect:?})"
            );
        }
        let live_time = col_with_db_type("start_time", "unknown", None, Some("time"));
        for dialect in [Dialect::Sqlite, Dialect::Postgres] {
            assert!(
                !schema_columns_storage_drift(&live_time, &model, dialect),
                "a live time column matches a time model ({dialect:?})"
            );
        }
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
    fn db_type_token_to_canonical_resolves_blob() {
        assert_eq!(
            db_type_token_to_canonical("blob", Dialect::Sqlite),
            Some(CanonicalType::Blob)
        );
        assert_eq!(
            db_type_token_to_canonical("blob", Dialect::Postgres),
            Some(CanonicalType::Blob)
        );
    }

    /// The one cross-consumer guard: the token a live SQLite BLOB normalizes to
    /// must satisfy BOTH planner consumers at once — `db_type_token_to_canonical`
    /// (the IR planner) resolves it to `Blob`, AND `sqlite_type_class` (the legacy
    /// planner's SQLite arm) classes it the same as the model's declared blob.
    /// This is why the token is "blob", not the canonical "bytea": `sqlite_type_class`
    /// only recognizes Blob for strings containing "blob". (#165)
    #[test]
    fn live_sqlite_blob_round_trips_through_both_consumers() {
        let token = information_schema_to_db_type_token("BLOB", None, Dialect::Sqlite);
        // IR-path consumer.
        assert_eq!(
            db_type_token_to_canonical(&token, Dialect::Sqlite),
            Some(CanonicalType::Blob)
        );
        // Legacy-path consumer: same class as the model's declared blob type.
        assert_eq!(
            sqlite_type_class(&token),
            sqlite_type_class(&sqlite_declared_type(CanonicalType::Blob))
        );
    }

    /// A live BLOB column must NOT drift against a `binary` (bytes) model column,
    /// while a genuine text↔blob difference still drifts. (#165)
    #[test]
    fn blob_model_does_not_drift_against_live_blob_on_sqlite() {
        // Live column: introspected via the real normalizer, logical_type "unknown".
        let live_token = information_schema_to_db_type_token("BLOB", None, Dialect::Sqlite);
        let live = col_with_db_type("data", "unknown", None, Some(&live_token));
        // Model column: Python SchemaIR compiler emits logical_type "binary", db_type None.
        let model = col_with_db_type("data", "binary", None, None);
        assert!(
            !schema_columns_storage_drift(&live, &model, Dialect::Sqlite),
            "a live BLOB column must not read as drifted against a bytes model"
        );

        // Genuine diff preserved: a live TEXT column vs the same bytes model DOES drift.
        let live_text = col_with_db_type("data", "unknown", None, Some("text"));
        assert!(
            schema_columns_storage_drift(&live_text, &model, Dialect::Sqlite),
            "a real text→blob difference must still be detected"
        );
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
        // domain logical_type tokens
        assert_eq!(canonical_from_parts("datetime", None, "", Dialect::Postgres), Ok(CanonicalType::TimestampTz));
        assert_eq!(canonical_from_parts("integer", None, "", Dialect::Sqlite), Ok(CanonicalType::Integer));
        // unknown is an error (CREATE path maps this to Varchar at its call site)
        assert!(canonical_from_parts("mystery", None, "", Dialect::Postgres).is_err());
    }

    #[test]
    fn logical_canonical_from_schema_column_ignores_explicit_db_type() {
        let col = SchemaColumn {
            name: "external_id".to_string(),
            logical_type: "uuid".to_string(),
            db_type: Some("text".to_string()),
            db_type_explicit: Some(true),
            nullable: false,
            primary_key: false,
            autoincrement: false,
            unique: false,
            index: false,
            default: None,
            format: Some("uuid".to_string()),
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: false,
        };
        assert_eq!(
            logical_canonical_from_schema_column(&col, Dialect::Postgres),
            Ok(CanonicalType::Uuid)
        );
        assert_eq!(
            canonical_from_schema_column(&col, Dialect::Postgres),
            Ok(CanonicalType::Text)
        );
    }

    #[test]
    fn logical_canonical_from_schema_column_errors_on_unknown() {
        let col = SchemaColumn {
            name: "mystery".to_string(),
            logical_type: "bogus".to_string(),
            db_type: None,
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
        };
        assert!(logical_canonical_from_schema_column(&col, Dialect::Postgres).is_err());
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

    fn sample_check() -> ferro_schema_ir::SchemaCheck {
        ferro_schema_ir::SchemaCheck {
            name: "ck_account_role".to_string(),
            column: "role".to_string(),
            values: vec!["'admin'".to_string(), "'user'".to_string()],
        }
    }

    #[test]
    fn render_db_check_postgres_is_idempotent_and_wraps_the_bare_alter() {
        let e = render_db_check("account", &sample_check(), Dialect::Postgres);
        // The idempotent guard: only ADD CONSTRAINT when pg_constraint lacks it,
        // so a re-run against an already-migrated schema is a no-op (G6, #176).
        assert_eq!(
            e.statement.as_deref(),
            Some(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint \
                 WHERE conname = 'ck_account_role' AND conrelid = '\"account\"'::regclass) THEN \
                 ALTER TABLE \"account\" ADD CONSTRAINT \"ck_account_role\" \
                 CHECK (\"role\" IN ('admin', 'user')); END IF; END $$"
            )
        );
        assert!(e.warning.is_none());
        // The CHECK body is embedded byte-identically (cross-emitter parity).
        assert!(
            e.statement
                .as_deref()
                .unwrap()
                .contains(&render_check_body(&sample_check()))
        );
    }

    #[test]
    fn render_db_check_sqlite_still_elides_with_warning() {
        let e = render_db_check("account", &sample_check(), Dialect::Sqlite);
        assert!(e.statement.is_none());
        assert!(e.warning.as_deref().unwrap().contains("ck_account_role"));
    }

    #[test]
    fn sqlite_type_classes_group_storage_equivalent_spellings() {
        for (a, b) in [
            ("DATETIME", "timestamp_with_timezone_text"),
            ("DATE", "date_text"),
            ("uuid_text", "char(32)"),
            ("JSON", "json_text"),
            ("NUMERIC", "real"),
            ("BOOLEAN", "integer"),
            ("BIGINT", "integer"),
            ("VARCHAR(3)", "varchar"),
        ] {
            assert_eq!(
                sqlite_type_class(a),
                sqlite_type_class(b),
                "{a} and {b} should be storage-equivalent"
            );
        }
        assert_ne!(sqlite_type_class("integer"), sqlite_type_class("varchar"));
        assert_ne!(sqlite_type_class("blob"), sqlite_type_class("text"));
    }

    #[test]
    fn fk_name_matches_i1_convention() {
        assert_eq!(
            fk_name("account", "org_id", "organization"),
            "fk_account_org_id_organization"
        );
    }

    #[test]
    fn fk_name_guards_identifiers_over_63_chars() {
        let table = "a".repeat(30);
        let col = "b".repeat(30);
        let to = "c".repeat(30);
        let raw = format!("fk_{table}_{col}_{to}");
        let name = fk_name(&table, &col, &to);
        assert_eq!(name.chars().count(), 63);
        assert_eq!(
            name,
            format!("{}_fk", raw.chars().take(60).collect::<String>())
        );
    }

    #[test]
    fn fk_name_guard_counts_chars_not_bytes() {
        // Multibyte identifiers must be truncated by char count, not byte offset
        // (a byte-offset slice would panic mid-codepoint).
        let table = "é".repeat(70);
        let name = fk_name(&table, "col", "other");
        assert_eq!(name.chars().count(), 63);
        assert!(name.ends_with("_fk"));
    }

    #[test]
    fn single_index_name_guards_identifiers_over_63_chars() {
        // Short names unchanged (pinned above in naming_helpers_match_i1_conventions).
        assert_eq!(single_index_name("user", "email"), "idx_user_email");
        let table = "t".repeat(40);
        let col = "c".repeat(30);
        let raw = format!("idx_{table}_{col}");
        let name = single_index_name(&table, &col);
        assert_eq!(name.chars().count(), 63);
        assert_eq!(
            name,
            format!("{}_idx", raw.chars().take(59).collect::<String>())
        );
    }

    fn enum_col(name: &str, type_name: Option<&str>, values: Vec<serde_json::Value>) -> SchemaColumn {
        SchemaColumn {
            enum_values: Some(values),
            enum_type_name: type_name.map(str::to_string),
            ..col_with_db_type(name, "string", None, None)
        }
    }

    #[test]
    fn resolve_column_storage_explicit_db_type_wins_over_enum() {
        let mut col = enum_col("role", Some("role"), vec![serde_json::json!("admin")]);
        col.db_type = Some("text".to_string());
        col.db_type_explicit = Some(true);
        assert_eq!(
            resolve_column_storage(&col, Dialect::Postgres),
            Ok(ResolvedStorage::Scalar(CanonicalType::Text))
        );
        // A NON-explicit db_type (e.g. a token back-filled by an adapter from
        // the old varchar lowering) must NOT mask enum resolution.
        col.db_type = Some("varchar".to_string());
        col.db_type_explicit = None;
        assert!(matches!(
            resolve_column_storage(&col, Dialect::Postgres),
            Ok(ResolvedStorage::PgEnum { .. })
        ));
    }

    #[test]
    fn resolve_column_storage_enum_is_native_pg_type() {
        let col = enum_col(
            "status",
            Some("status"),
            vec![serde_json::json!("draft"), serde_json::json!("active")],
        );
        assert_eq!(
            resolve_column_storage(&col, Dialect::Postgres),
            Ok(ResolvedStorage::PgEnum {
                type_name: "status".to_string(),
                labels: vec!["draft".to_string(), "active".to_string()],
            })
        );
    }

    #[test]
    fn resolve_column_storage_enum_type_name_falls_back_to_column_name() {
        let col = enum_col("status", None, vec![serde_json::json!("draft")]);
        match resolve_column_storage(&col, Dialect::Postgres) {
            Ok(ResolvedStorage::PgEnum { type_name, .. }) => assert_eq!(type_name, "status"),
            other => panic!("expected PgEnum, got {other:?}"),
        }
    }

    #[test]
    fn resolve_column_storage_int_enum_labels_are_stringified() {
        // Pinned by the already-shipped bridge behavior
        // (test_standard_enum_generates_with_name: labels {"1","2","3"}).
        let col = enum_col(
            "priority",
            Some("priority"),
            vec![serde_json::json!(1), serde_json::json!(2), serde_json::json!(3)],
        );
        match resolve_column_storage(&col, Dialect::Postgres) {
            Ok(ResolvedStorage::PgEnum { labels, .. }) => {
                assert_eq!(labels, vec!["1", "2", "3"]);
            }
            other => panic!("expected PgEnum, got {other:?}"),
        }
    }

    #[test]
    fn resolve_column_storage_enum_on_sqlite_is_varchar_max_label_len() {
        // Byte-matches what SQLAlchemy renders for sa.Enum on SQLite:
        // VARCHAR(<longest label length>).
        let col = enum_col(
            "status",
            Some("status"),
            vec![serde_json::json!("draft"), serde_json::json!("archived")],
        );
        assert_eq!(
            resolve_column_storage(&col, Dialect::Sqlite),
            Ok(ResolvedStorage::Scalar(CanonicalType::Varchar(Some(8))))
        );
    }

    #[test]
    fn resolve_column_storage_scalars_follow_canonical_from_parts() {
        let dt = col_with_db_type("created_at", "datetime", Some("date-time"), None);
        assert_eq!(
            resolve_column_storage(&dt, Dialect::Postgres),
            Ok(ResolvedStorage::Scalar(CanonicalType::TimestampTz))
        );
        let unknown = col_with_db_type("x", "mystery", None, None);
        assert!(resolve_column_storage(&unknown, Dialect::Postgres).is_err());
    }

    #[test]
    fn time_logical_type_resolves_to_time_on_both_dialects() {
        // FF-B B2 (D3): `datetime.time` fields store as `time`, resolving the
        // #141 asymmetry (previously Varchar so the consume side agreed with
        // the old varchar-emitting create path).
        assert_eq!(
            canonical_from_parts("time", None, "", Dialect::Sqlite),
            Ok(CanonicalType::Time)
        );
        assert_eq!(
            canonical_from_parts("time", None, "", Dialect::Postgres),
            Ok(CanonicalType::Time)
        );
    }

    #[test]
    fn render_pg_enum_create_type_is_idempotent_and_pinned() {
        let sql = render_pg_enum_create_type(
            "status",
            &["draft".to_string(), "active".to_string()],
        );
        assert_eq!(
            sql,
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type t \
             JOIN pg_namespace n ON n.oid = t.typnamespace \
             WHERE t.typname = 'status' AND n.nspname = current_schema()) THEN \
             CREATE TYPE \"status\" AS ENUM ('draft', 'active'); \
             END IF; END $$"
        );
    }

    #[test]
    fn missing_enum_labels_returns_additions_in_declared_order() {
        let declared = vec!["plaid".to_string(), "mx".to_string(), "teller".to_string()];
        let live = vec!["plaid".to_string()];
        assert_eq!(missing_enum_labels(&declared, &live), vec!["mx", "teller"]);
    }

    #[test]
    fn missing_enum_labels_empty_when_live_covers_declared() {
        let declared = vec!["plaid".to_string()];
        let live = vec!["plaid".to_string(), "legacy".to_string()];
        assert!(missing_enum_labels(&declared, &live).is_empty());
    }

    #[test]
    fn extra_enum_labels_returns_undeclared_live_labels_in_live_order() {
        let declared = vec!["plaid".to_string()];
        let live = vec!["legacy".to_string(), "plaid".to_string(), "old".to_string()];
        assert_eq!(extra_enum_labels(&declared, &live), vec!["legacy", "old"]);
        assert!(extra_enum_labels(&live, &live).is_empty());
    }

    #[test]
    fn extra_enum_labels_warning_is_pinned_and_names_the_exit() {
        assert_eq!(
            extra_enum_labels_warning("provider", &["legacy".to_string()]),
            Some(
                "Enum type 'provider' has label(s) 'legacy' that the model no longer \
                 declares. Label addition is append-only: ferro never removes enum \
                 labels (existing rows may still hold them). Remove or rename labels \
                 with a reviewed Alembic migration."
                    .to_string()
            )
        );
        assert_eq!(extra_enum_labels_warning("provider", &[]), None);
    }

    #[test]
    fn render_pg_enum_add_value_is_pinned_and_escapes() {
        assert_eq!(
            render_pg_enum_add_value("provider", "mx"),
            "ALTER TYPE \"provider\" ADD VALUE IF NOT EXISTS 'mx'"
        );
        assert_eq!(
            render_pg_enum_add_value("od'd", "it's"),
            "ALTER TYPE \"od'd\" ADD VALUE IF NOT EXISTS 'it''s'"
        );
    }

    #[test]
    fn render_pg_enum_create_type_escapes_quotes() {
        let sql = render_pg_enum_create_type("od'd", &["it's".to_string()]);
        assert!(sql.contains("t.typname = 'od''d'"), "{sql}");
        assert!(sql.contains("AS ENUM ('it''s')"), "{sql}");
    }

    #[test]
    fn refused_conversion_detects_varchar_to_pg_enum() {
        let live = col_with_db_type("role", "unknown", None, Some("varchar"));
        let target = ResolvedStorage::PgEnum {
            type_name: "role".to_string(),
            labels: vec!["admin".to_string()],
        };
        assert_eq!(
            refused_conversion(&live, &target, Dialect::Postgres),
            Some(RefusedConversion::VarcharToPgEnum)
        );
        // A live native-enum column is NOT a refusal (same storage; no-op).
        let mut native = col_with_db_type("role", "unknown", None, Some("varchar"));
        native.postgres_native_enum = true;
        assert_eq!(refused_conversion(&native, &target, Dialect::Postgres), None);
        // Scalar targets delegate to the scalar rail.
        let live_ts = col_with_db_type("at", "unknown", None, Some("timestamp"));
        assert_eq!(
            refused_conversion(
                &live_ts,
                &ResolvedStorage::Scalar(CanonicalType::TimestampTz),
                Dialect::Postgres
            ),
            Some(RefusedConversion::TimestampTz)
        );
    }

    #[test]
    fn enum_model_drifts_against_live_varchar_but_not_native_enum() {
        let live_varchar = col_with_db_type("role", "unknown", None, Some("varchar"));
        let model = enum_col("role", Some("role"), vec![serde_json::json!("admin")]);
        assert!(
            schema_columns_storage_drift(&live_varchar, &model, Dialect::Postgres),
            "varchar-stored enum must surface as drift (refused at emission)"
        );
        let mut live_native = col_with_db_type("role", "unknown", None, Some("varchar"));
        live_native.postgres_native_enum = true;
        assert!(
            !schema_columns_storage_drift(&live_native, &model, Dialect::Postgres),
            "a live native-enum column matches an enum model — second boot is a no-op"
        );
    }

    #[test]
    fn refused_scalar_conversion_detects_tz_and_time_pairs() {
        use CanonicalType::*;
        assert_eq!(
            refused_scalar_conversion(Timestamp, TimestampTz),
            Some(RefusedConversion::TimestampTz)
        );
        assert_eq!(
            refused_scalar_conversion(TimestampTz, DateTime),
            Some(RefusedConversion::TimestampTz)
        );
        // Old lowering stored `datetime.time` fields as varchar; the model now
        // targets `time`. Refused: the cast can fail or reinterpret stored text.
        assert_eq!(
            refused_scalar_conversion(Varchar(None), Time),
            Some(RefusedConversion::VarcharToTime)
        );
        assert_eq!(
            refused_scalar_conversion(Text, Time),
            Some(RefusedConversion::VarcharToTime)
        );
        // Ordinary widenings and unrelated pairs are not refusals.
        assert_eq!(refused_scalar_conversion(Integer, BigInt), None);
        assert_eq!(refused_scalar_conversion(Time, Varchar(None)), None);
        assert_eq!(refused_scalar_conversion(Varchar(None), Text), None);
    }

    #[test]
    fn refused_conversion_warning_tz_matches_the_shadow_pinned_text() {
        // The TimestampTz arm must return the exact legacy warning bytes — the
        // shadow comparator asserts warning equality between planner paths.
        let via_kind = refused_conversion_warning(
            RefusedConversion::TimestampTz,
            "event",
            "occurred_at",
            "timestamp",
            "timestamptz",
            "timestamp",
        );
        let direct = timestamp_tz_conversion_warning(
            "event",
            "occurred_at",
            "timestamp",
            "timestamptz",
            "timestamp",
        );
        assert_eq!(via_kind, direct);
    }

    #[test]
    fn refused_conversion_warning_enum_names_column_db_type_and_alembic() {
        let w = refused_conversion_warning(
            RefusedConversion::VarcharToPgEnum,
            "account",
            "role",
            "varchar",
            "role",
            "varchar",
        );
        let col = w.find("account.role").expect("names the column");
        let dbt = w.find("db_type").expect("names db_type");
        let alembic = w.find("Alembic").expect("names Alembic");
        assert!(
            col < dbt && dbt < alembic,
            "tokens must appear in order: {w}"
        );
        assert!(
            w.contains("USING"),
            "enum recipe points at a USING cast: {w}"
        );
    }

    #[test]
    fn refused_conversion_warning_time_names_column_db_type_and_alembic() {
        let w = refused_conversion_warning(
            RefusedConversion::VarcharToTime,
            "shift",
            "opens_at",
            "varchar",
            "time",
            "varchar",
        );
        let col = w.find("shift.opens_at").expect("names the column");
        let dbt = w.find("db_type").expect("names db_type");
        let alembic = w.find("Alembic").expect("names Alembic");
        assert!(
            col < dbt && dbt < alembic,
            "tokens must appear in order: {w}"
        );
    }

    // -----------------------------------------------------------------------
    // Row security (#409). These pins ARE the cross-emitter contract: the
    // runtime create pass and (from #414) the Alembic operation both render
    // through these functions, so a change here is a change to both doors.
    // -----------------------------------------------------------------------

    fn rls_model(columns: Vec<SchemaColumn>) -> ferro_schema_ir::SchemaModel {
        ferro_schema_ir::SchemaModel {
            model_name: "LedgerRow".to_string(),
            table_name: "ledgerrow".to_string(),
            columns,
            foreign_keys: vec![],
            indexes: vec![],
            uniques: vec![],
            checks: vec![],
            table_checks: vec![],
            row_security: None,
        }
    }

    fn setting_policy(
        name: &str,
        column: &str,
        setting: &str,
        command: ferro_schema_ir::RowPolicyCommand,
        restrictive: bool,
    ) -> ferro_schema_ir::SchemaRowPolicy {
        ferro_schema_ir::SchemaRowPolicy {
            name: name.to_string(),
            command,
            restrictive,
            expr: ferro_schema_ir::RowPolicyExpr::Setting {
                column: column.to_string(),
                setting: setting.to_string(),
            },
        }
    }

    #[test]
    fn row_policy_name_matches_the_ferro_owned_prefix() {
        assert_eq!(
            row_policy_name("ledgerrow", "ledger_id"),
            "rls_ledgerrow_ledger_id"
        );
        assert!(is_ferro_row_policy_name("rls_ledgerrow_ledger_id"));
        assert!(!is_ferro_row_policy_name("tenant_isolation"));
    }

    #[test]
    fn row_policy_name_truncates_above_63() {
        let name = row_policy_name("verylongtable", &"a".repeat(70));
        assert_eq!(name.chars().count(), 63);
        assert!(name.ends_with("_rls"));
    }

    #[test]
    fn shorthand_cast_allowlist_is_uuid_text_and_the_integer_families() {
        let uuid = col_with_db_type("ledger_id", "uuid", None, None);
        assert_eq!(
            row_policy_shorthand_cast(&uuid).unwrap(),
            Some("uuid".into())
        );

        let text = col_with_db_type("tenant", "string", None, None);
        assert_eq!(row_policy_shorthand_cast(&text).unwrap(), None);

        let varchar = col_with_db_type("tenant", "string", None, Some("varchar(40)"));
        assert_eq!(row_policy_shorthand_cast(&varchar).unwrap(), None);

        let int = col_with_db_type("tenant_id", "integer", None, None);
        assert_eq!(
            row_policy_shorthand_cast(&int).unwrap(),
            Some("integer".into())
        );
        let big = col_with_db_type("tenant_id", "integer", None, Some("bigint"));
        assert_eq!(
            row_policy_shorthand_cast(&big).unwrap(),
            Some("bigint".into())
        );
        let small = col_with_db_type("tenant_id", "integer", None, Some("smallint"));
        assert_eq!(
            row_policy_shorthand_cast(&small).unwrap(),
            Some("smallint".into())
        );
    }

    #[test]
    fn shorthand_cast_rejects_every_other_storage() {
        let ts = col_with_db_type("created_at", "datetime", None, None);
        let err = row_policy_shorthand_cast(&ts).unwrap_err();
        assert!(err.contains("created_at"), "{err}");
        assert!(err.contains("timestamptz"), "{err}");
        assert!(err.contains("does not support"), "{err}");

        let flag = col_with_db_type("active", "boolean", None, None);
        assert!(row_policy_shorthand_cast(&flag).is_err());

        let native_enum = enum_col(
            "role",
            Some("role_enum"),
            vec![serde_json::json!("admin"), serde_json::json!("user")],
        );
        let err = row_policy_shorthand_cast(&native_enum).unwrap_err();
        assert!(err.contains("role_enum"), "{err}");
    }

    #[test]
    fn shorthand_expr_wraps_current_setting_in_nullif() {
        // The exact shape the PRD pins: a set-then-RESET GUC reads back as '',
        // and NULLIF is what keeps `''::uuid` from erroring on every query.
        assert_eq!(
            render_row_policy_setting_expr("ledger_id", "pinch.ledger_id", Some("uuid")),
            "\"ledger_id\" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid"
        );
        assert_eq!(
            render_row_policy_setting_expr("tenant", "pinch.tenant", None),
            "\"tenant\" = NULLIF(current_setting('pinch.tenant', true), '')"
        );
    }

    #[test]
    fn create_policy_renders_for_all_with_both_clauses() {
        let model = rls_model(vec![col_with_db_type("ledger_id", "uuid", None, None)]);
        let policy = setting_policy(
            "rls_ledgerrow_ledger_id",
            "ledger_id",
            "pinch.ledger_id",
            ferro_schema_ir::RowPolicyCommand::All,
            false,
        );
        assert_eq!(
            render_create_row_policy(&model, &policy).unwrap(),
            "CREATE POLICY \"rls_ledgerrow_ledger_id\" ON \"ledgerrow\" FOR ALL \
             USING (\"ledger_id\" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid) \
             WITH CHECK (\"ledger_id\" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid)"
        );
    }

    #[test]
    fn create_policy_restrictive_renders_as_restrictive() {
        let model = rls_model(vec![col_with_db_type("ledger_id", "uuid", None, None)]);
        let policy = setting_policy(
            "rls_ledgerrow_ledger_id",
            "ledger_id",
            "pinch.ledger_id",
            ferro_schema_ir::RowPolicyCommand::All,
            true,
        );
        assert!(
            render_create_row_policy(&model, &policy)
                .unwrap()
                .starts_with(
                    "CREATE POLICY \"rls_ledgerrow_ledger_id\" ON \"ledgerrow\" AS RESTRICTIVE FOR ALL "
                )
        );
    }

    #[test]
    fn create_policy_drops_the_clause_its_command_cannot_take() {
        // Postgres rejects WITH CHECK on SELECT/DELETE and USING on INSERT;
        // the shorthand renders one expression and this decision filters it.
        let model = rls_model(vec![col_with_db_type("tenant", "string", None, None)]);
        let expr = "\"tenant\" = NULLIF(current_setting('pinch.tenant', true), '')";

        for (command, expected) in [
            (
                ferro_schema_ir::RowPolicyCommand::Select,
                format!(
                    "CREATE POLICY \"rls_ledgerrow_tenant\" ON \"ledgerrow\" FOR SELECT USING ({expr})"
                ),
            ),
            (
                ferro_schema_ir::RowPolicyCommand::Delete,
                format!(
                    "CREATE POLICY \"rls_ledgerrow_tenant\" ON \"ledgerrow\" FOR DELETE USING ({expr})"
                ),
            ),
            (
                ferro_schema_ir::RowPolicyCommand::Insert,
                format!(
                    "CREATE POLICY \"rls_ledgerrow_tenant\" ON \"ledgerrow\" FOR INSERT WITH CHECK ({expr})"
                ),
            ),
        ] {
            let policy = setting_policy(
                "rls_ledgerrow_tenant",
                "tenant",
                "pinch.tenant",
                command,
                false,
            );
            assert_eq!(render_create_row_policy(&model, &policy).unwrap(), expected);
        }
    }

    #[test]
    fn create_policy_renders_raw_expressions_verbatim() {
        let model = rls_model(vec![col_with_db_type("id", "uuid", None, None)]);
        let policy = ferro_schema_ir::SchemaRowPolicy {
            name: "rls_ledgerrow_invitee_read".to_string(),
            command: ferro_schema_ir::RowPolicyCommand::Select,
            restrictive: false,
            expr: ferro_schema_ir::RowPolicyExpr::Raw {
                using: Some("id IN (SELECT row_id FROM membership)".to_string()),
                with_check: None,
            },
        };
        assert_eq!(
            render_create_row_policy(&model, &policy).unwrap(),
            "CREATE POLICY \"rls_ledgerrow_invitee_read\" ON \"ledgerrow\" FOR SELECT \
             USING (id IN (SELECT row_id FROM membership))"
        );
    }

    #[test]
    fn row_security_statements_order_enable_force_then_policies() {
        let mut model = rls_model(vec![col_with_db_type("ledger_id", "uuid", None, None)]);
        model.row_security = Some(ferro_schema_ir::SchemaRowSecurity {
            force: true,
            policies: vec![setting_policy(
                "rls_ledgerrow_ledger_id",
                "ledger_id",
                "pinch.ledger_id",
                ferro_schema_ir::RowPolicyCommand::All,
                false,
            )],
        });
        let emission = row_security_statements(&model, Dialect::Postgres).unwrap();
        assert_eq!(
            emission.statements[0],
            "ALTER TABLE \"ledgerrow\" ENABLE ROW LEVEL SECURITY"
        );
        assert_eq!(
            emission.statements[1],
            "ALTER TABLE \"ledgerrow\" FORCE ROW LEVEL SECURITY"
        );
        assert!(emission.statements[2].starts_with("CREATE POLICY"));
        assert_eq!(emission.statements.len(), 3);
        assert!(emission.warning.is_none());
    }

    #[test]
    fn row_security_statements_omit_force_when_not_declared() {
        let mut model = rls_model(vec![col_with_db_type("ledger_id", "uuid", None, None)]);
        model.row_security = Some(ferro_schema_ir::SchemaRowSecurity {
            force: false,
            policies: vec![],
        });
        let emission = row_security_statements(&model, Dialect::Postgres).unwrap();
        assert_eq!(
            emission.statements,
            vec!["ALTER TABLE \"ledgerrow\" ENABLE ROW LEVEL SECURITY".to_string()]
        );
    }

    #[test]
    fn row_security_statements_are_one_warning_and_no_ddl_on_sqlite() {
        let mut model = rls_model(vec![col_with_db_type("ledger_id", "uuid", None, None)]);
        model.row_security = Some(ferro_schema_ir::SchemaRowSecurity {
            force: true,
            policies: vec![
                setting_policy(
                    "rls_ledgerrow_ledger_id",
                    "ledger_id",
                    "pinch.ledger_id",
                    ferro_schema_ir::RowPolicyCommand::All,
                    false,
                ),
                setting_policy(
                    "rls_ledgerrow_second",
                    "ledger_id",
                    "pinch.other",
                    ferro_schema_ir::RowPolicyCommand::Select,
                    false,
                ),
            ],
        });
        let emission = row_security_statements(&model, Dialect::Sqlite).unwrap();
        assert!(emission.statements.is_empty());
        let warning = emission
            .warning
            .expect("SQLite warns, never silently skips");
        assert!(warning.contains("ledgerrow"), "{warning}");
        assert!(warning.contains("PostgreSQL-only"), "{warning}");
    }

    #[test]
    fn row_security_statements_are_empty_without_a_declaration() {
        let model = rls_model(vec![col_with_db_type("ledger_id", "uuid", None, None)]);
        let emission = row_security_statements(&model, Dialect::Postgres).unwrap();
        assert!(emission.statements.is_empty());
        assert!(emission.warning.is_none());
    }

    #[test]
    fn row_security_statements_fail_loudly_on_an_unknown_column() {
        let mut model = rls_model(vec![col_with_db_type("ledger_id", "uuid", None, None)]);
        model.row_security = Some(ferro_schema_ir::SchemaRowSecurity {
            force: true,
            policies: vec![setting_policy(
                "rls_ledgerrow_missing",
                "missing",
                "pinch.ledger_id",
                ferro_schema_ir::RowPolicyCommand::All,
                false,
            )],
        });
        let err = row_security_statements(&model, Dialect::Postgres).unwrap_err();
        assert!(err.contains("missing"), "{err}");
    }
}
