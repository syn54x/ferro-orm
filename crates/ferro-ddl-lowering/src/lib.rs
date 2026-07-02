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
            CanonicalType::Json => Some("JSON"),
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
        // FF-B B2: `datetime.time` stores as `time` on both dialects (the #141
        // varchar asymmetry is resolved the other way). Legacy varchar-stored
        // columns surface as drift and are REFUSED at emission
        // (`RefusedConversion::VarcharToTime`) — never silently altered.
        ("time", _) | ("string", Some("time")) => Ok(CanonicalType::Time),
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
        CanonicalType::Json => "JSON".to_string(),
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
        // Model derived (post-#141 wire IR): db_type None, format "uuid" → Uuid.
        let model = col_with_db_type("id", "string", Some("uuid"), None);
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
        // The raw JSON-Schema spelling used by the legacy planner path.
        assert_eq!(
            canonical_from_parts("string", Some("time"), "", Dialect::Postgres),
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
}
