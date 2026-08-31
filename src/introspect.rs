//! Live database schema introspection.
//!
//! Reads the structure of existing tables so the auto-migrate diff
//! (`src/migrate.rs`) can compare them against registered model schemas.
//! All queries run unprepared — introspection precedes DDL, and neither may
//! populate a connection's statement cache (see `EngineHandle::execute_sql_unprepared`).

use crate::backend::{EngineBindValue, EngineHandle, EngineRow, EngineValue};
use crate::state::Dialect;
use ferro_ddl_lowering::{
    LiveRowPolicy, LiveRowSecurity, is_ferro_row_policy_name,
    row_policy_command_from_catalog_code,
};
use pyo3::prelude::*;

fn serde_default_true() -> bool {
    true
}

/// One column of a live database table, normalized across backends.
///
/// `Deserialize` exists for the `_render_migration_sql_for_test` helper,
/// which accepts live columns as JSON so the diff can be exercised without a
/// database.
#[derive(Clone, Debug, serde::Deserialize)]
pub struct LiveColumn {
    pub name: String,
    /// SQLite: the declared type from `PRAGMA table_info` (e.g. `varchar`,
    /// `uuid_text`, `datetime_text`). Postgres: `information_schema.data_type`
    /// (e.g. `character varying`, `timestamp with time zone`).
    #[serde(default)]
    pub declared_type: String,
    #[serde(default = "serde_default_true")]
    pub is_nullable: bool,
    #[serde(default)]
    pub is_primary_key: bool,
    /// Postgres `character_maximum_length`; always `None` on SQLite (declared
    /// lengths live inside `declared_type`, e.g. `varchar(40)`).
    #[serde(default)]
    pub char_max_len: Option<i64>,
    /// Postgres: the column's type is a native enum (`pg_type.typtype = 'e'`).
    /// Such columns are Alembic-managed and excluded from type reconciliation.
    #[serde(default)]
    pub is_enum_udt: bool,
}

/// One live standalone index that Ferro owns (its name follows the `idx_`/`uq_`
/// convention). Deserialize exists for `_render_migration_sql_for_test`.
#[derive(Clone, Debug, serde::Deserialize)]
pub struct LiveIndex {
    pub name: String,
    #[serde(default)]
    pub columns: Vec<String>,
    #[serde(default)]
    pub unique: bool,
}

/// Ferro emits standalone indexes as `idx_<table>_<cols>` and uniques as
/// `uq_<table>_<cols>`. Reconciliation only ever touches names it owns.
pub(crate) fn is_ferro_index_name(name: &str) -> bool {
    name.starts_with("idx_") || name.starts_with("uq_")
}

/// One live CHECK constraint on a table, normalized across backends.
///
/// `definition` is the catalog rendering: Postgres `pg_get_constraintdef`
/// (`CHECK (...)`), SQLite the inline `CHECK (...)` fragment from
/// `sqlite_master.sql`. Returned as-is — body-drift normalization is #344.
///
/// `ferro_owned` is true when the constraint name follows the `ck_*`
/// convention; user-owned CHECKs are included so reconciliation can skip them.
#[derive(Clone, Debug, serde::Deserialize)]
pub struct LiveCheck {
    pub name: String,
    pub definition: String,
    #[serde(default)]
    pub ferro_owned: bool,
}

/// Ferro emits table and column CHECKs as `ck_<table>_<suffix>`. Reconciliation
/// only ever touches names it owns.
pub(crate) fn is_ferro_check_name(name: &str) -> bool {
    name.starts_with("ck_")
}

/// One live single-column foreign-key constraint, normalized across backends.
/// (Ferro only ever emits single-column FKs; multi-column live constraints are
/// user-owned by construction and never surfaced to reconciliation.)
///
/// `Deserialize` exists for `_render_migration_sql_for_test`.
#[derive(Clone, Debug, serde::Deserialize)]
pub struct LiveForeignKey {
    /// Constraint name. `None` on SQLite — `PRAGMA foreign_key_list` does not
    /// expose constraint names (one more way SQLite FK constraints cannot be
    /// reconciled in place, only warned about).
    #[serde(default)]
    pub name: Option<String>,
    /// Local column.
    pub column: String,
    /// Referenced table.
    pub to_table: String,
    /// Referenced column.
    #[serde(default)]
    pub to_column: String,
    /// `ON DELETE` action in the declared-IR vocabulary: `CASCADE`,
    /// `SET NULL`, `SET DEFAULT`, `RESTRICT`, `NO ACTION`.
    pub on_delete: String,
}

/// Map `pg_constraint.confdeltype` to the declared-IR action vocabulary.
/// Returns `None` for codes Postgres does not produce.
pub(crate) fn fk_action_from_confdeltype(confdeltype: &str) -> Option<&'static str> {
    match confdeltype {
        "a" => Some("NO ACTION"),
        "r" => Some("RESTRICT"),
        "c" => Some("CASCADE"),
        "n" => Some("SET NULL"),
        "d" => Some("SET DEFAULT"),
        _ => None,
    }
}

/// One live SQLite index covering some column, with enough context to decide
/// whether it can be dropped ahead of `ALTER TABLE ... DROP COLUMN`.
#[derive(Clone, Debug)]
pub struct SqliteIndex {
    pub name: String,
    /// `PRAGMA index_list` origin: `"c"` = explicit `CREATE INDEX` (droppable),
    /// `"u"` = UNIQUE-constraint autoindex, `"pk"` = PRIMARY KEY autoindex
    /// (neither autoindex can be dropped with `DROP INDEX`).
    pub origin: String,
}

fn row_string(row: &EngineRow, column: &str) -> Option<String> {
    row.values
        .iter()
        .find(|(name, _)| name == column)
        .and_then(|(_, value)| match value {
            EngineValue::String(value) => Some(value.clone()),
            EngineValue::I64(value) => Some(value.to_string()),
            _ => None,
        })
}

fn row_bool(row: &EngineRow, column: &str) -> bool {
    row.values
        .iter()
        .find(|(name, _)| name == column)
        .map(|(_, value)| match value {
            EngineValue::Bool(value) => *value,
            EngineValue::I64(value) => *value != 0,
            _ => false,
        })
        .unwrap_or(false)
}

fn row_opt_i64(row: &EngineRow, column: &str) -> Option<i64> {
    row.values
        .iter()
        .find(|(name, _)| name == column)
        .and_then(|(_, value)| value.as_i64())
}

/// Quote an identifier for direct inclusion in SQL (`PRAGMA` arguments cannot
/// be bound as parameters).
pub(crate) fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

fn introspection_error(context: &str, table: &str, err: sqlx::Error) -> PyErr {
    crate::errors::map_db_error(
        &format!("Schema introspection failed ({context} for table '{table}')"),
        err,
    )
}

/// Read the live columns of `table`. Returns `None` when the table does not
/// exist (a table cannot have zero columns on either backend).
pub async fn live_table_columns(
    engine: &EngineHandle,
    table: &str,
) -> PyResult<Option<Vec<LiveColumn>>> {
    let columns = match engine.backend() {
        Dialect::Sqlite => sqlite_table_columns(engine, table).await?,
        Dialect::Postgres => postgres_table_columns(engine, table).await?,
    };
    Ok(if columns.is_empty() {
        None
    } else {
        Some(columns)
    })
}

/// The set of live table names in the connected schema — one query, taken by
/// the create pass so it can leave every existing table to the reconciliation
/// pass (ADR-0010) instead of leaning on `IF NOT EXISTS` per statement.
pub async fn live_table_names(
    engine: &EngineHandle,
) -> PyResult<std::collections::HashSet<String>> {
    let (sql, context) = match engine.backend() {
        Dialect::Sqlite => (
            "SELECT name FROM sqlite_master WHERE type = 'table'",
            "sqlite_master",
        ),
        Dialect::Postgres => (
            "SELECT table_name::text AS name FROM information_schema.tables \
             WHERE table_schema = current_schema()",
            "information_schema.tables",
        ),
    };
    let rows = engine
        .fetch_all_sql_unprepared(sql)
        .await
        .map_err(|e| introspection_error(context, "*", e))?;
    Ok(rows
        .iter()
        .filter_map(|row| row_string(row, "name"))
        .collect())
}

/// Read every native enum type in the connected schema with its labels in
/// enum sort order: `type name → labels`. Postgres-only — SQLite has no native
/// enum types — and taken once per reconciliation run (label addition is
/// per-type, not per-table). Callers must guard on dialect.
pub async fn live_enum_type_labels(
    engine: &EngineHandle,
) -> PyResult<std::collections::BTreeMap<String, Vec<String>>> {
    let sql = "SELECT t.typname::text AS type_name, e.enumlabel::text AS label \
               FROM pg_type t \
               JOIN pg_namespace n ON n.oid = t.typnamespace \
               JOIN pg_enum e ON e.enumtypid = t.oid \
               WHERE n.nspname = current_schema() \
               ORDER BY t.typname, e.enumsortorder";
    let rows = engine
        .fetch_all_sql_unprepared(sql)
        .await
        .map_err(|e| introspection_error("pg_enum", "*", e))?;
    let mut labels_by_type: std::collections::BTreeMap<String, Vec<String>> =
        std::collections::BTreeMap::new();
    for row in &rows {
        if let (Some(type_name), Some(label)) =
            (row_string(row, "type_name"), row_string(row, "label"))
        {
            labels_by_type.entry(type_name).or_default().push(label);
        }
    }
    Ok(labels_by_type)
}

/// Read the live single-column foreign-key constraints on `table`.
pub async fn live_table_foreign_keys(
    engine: &EngineHandle,
    table: &str,
) -> PyResult<Vec<LiveForeignKey>> {
    match engine.backend() {
        Dialect::Sqlite => sqlite_table_foreign_keys(engine, table).await,
        Dialect::Postgres => postgres_table_foreign_keys(engine, table).await,
    }
}

async fn sqlite_table_foreign_keys(
    engine: &EngineHandle,
    table: &str,
) -> PyResult<Vec<LiveForeignKey>> {
    let sql = format!("PRAGMA foreign_key_list({})", quote_ident(table));
    let rows = engine
        .fetch_all_sql_unprepared(&sql)
        .await
        .map_err(|e| introspection_error("PRAGMA foreign_key_list", table, e))?;

    // Rows are (id, seq, table, from, to, on_update, on_delete, match); a
    // multi-column FK repeats its `id` with seq > 0 — those are user-owned by
    // construction and skipped whole.
    let multi_column: std::collections::HashSet<i64> = rows
        .iter()
        .filter(|row| row_opt_i64(row, "seq").unwrap_or(0) > 0)
        .filter_map(|row| row_opt_i64(row, "id"))
        .collect();

    Ok(rows
        .iter()
        .filter(|row| row_opt_i64(row, "seq").unwrap_or(0) == 0)
        .filter(|row| row_opt_i64(row, "id").map_or(true, |id| !multi_column.contains(&id)))
        .filter_map(|row| {
            Some(LiveForeignKey {
                name: None,
                column: row_string(row, "from")?,
                to_table: row_string(row, "table")?,
                // `to` is NULL when the FK references the target's implicit PK.
                to_column: row_string(row, "to").unwrap_or_default(),
                on_delete: row_string(row, "on_delete")
                    .unwrap_or_else(|| "NO ACTION".to_string())
                    .to_uppercase(),
            })
        })
        .collect())
}

async fn postgres_table_foreign_keys(
    engine: &EngineHandle,
    table: &str,
) -> PyResult<Vec<LiveForeignKey>> {
    let sql = r#"
        SELECT
            con.conname::text AS name,
            src.attname::text AS column_name,
            rel_f.relname::text AS to_table,
            dst.attname::text AS to_column,
            con.confdeltype::text AS on_delete,
            array_length(con.conkey, 1)::bigint AS n_cols
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        JOIN pg_class rel_f ON rel_f.oid = con.confrelid
        LEFT JOIN pg_attribute src
            ON src.attrelid = con.conrelid AND src.attnum = con.conkey[1]
        LEFT JOIN pg_attribute dst
            ON dst.attrelid = con.confrelid AND dst.attnum = con.confkey[1]
        WHERE con.contype = 'f'
          AND nsp.nspname = current_schema()
          AND rel.relname = $1
        ORDER BY con.conname
        "#;

    let rows = engine
        .fetch_all_sql_unprepared_with_binds(sql, &[EngineBindValue::String(table.to_string())])
        .await
        .map_err(|e| introspection_error("pg_constraint", table, e))?;

    Ok(rows
        .iter()
        .filter(|row| row_opt_i64(row, "n_cols") == Some(1))
        .filter_map(|row| {
            Some(LiveForeignKey {
                name: row_string(row, "name"),
                column: row_string(row, "column_name")?,
                to_table: row_string(row, "to_table")?,
                to_column: row_string(row, "to_column").unwrap_or_default(),
                on_delete: fk_action_from_confdeltype(&row_string(row, "on_delete")?)?.to_string(),
            })
        })
        .collect())
}

async fn sqlite_table_columns(engine: &EngineHandle, table: &str) -> PyResult<Vec<LiveColumn>> {
    let sql = format!("PRAGMA table_info({})", quote_ident(table));
    let rows = engine
        .fetch_all_sql_unprepared(&sql)
        .await
        .map_err(|e| introspection_error("PRAGMA table_info", table, e))?;

    Ok(rows
        .iter()
        .filter_map(|row| {
            let name = row_string(row, "name")?;
            Some(LiveColumn {
                name,
                declared_type: row_string(row, "type").unwrap_or_default(),
                is_nullable: row_opt_i64(row, "notnull") == Some(0),
                // `pk` is the 1-based position within the primary key (0 = not part of it).
                is_primary_key: row_opt_i64(row, "pk").unwrap_or(0) > 0,
                char_max_len: None,
                is_enum_udt: false,
            })
        })
        .collect())
}

async fn postgres_table_columns(engine: &EngineHandle, table: &str) -> PyResult<Vec<LiveColumn>> {
    let sql = r#"
        SELECT
            c.column_name::text AS column_name,
            c.data_type::text AS data_type,
            (c.is_nullable = 'YES') AS is_nullable,
            c.character_maximum_length::bigint AS char_max_len,
            EXISTS (
                SELECT 1
                FROM pg_attribute a
                JOIN pg_class cl ON a.attrelid = cl.oid
                JOIN pg_namespace n ON cl.relnamespace = n.oid
                JOIN pg_type t ON a.atttypid = t.oid
                WHERE n.nspname = c.table_schema
                  AND cl.relname = c.table_name
                  AND a.attname = c.column_name
                  AND t.typtype = 'e'
            ) AS is_enum_udt,
            EXISTS (
                SELECT 1
                FROM pg_index i
                JOIN pg_class cl ON i.indrelid = cl.oid
                JOIN pg_namespace n ON cl.relnamespace = n.oid
                JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum = ANY(i.indkey)
                WHERE n.nspname = c.table_schema
                  AND cl.relname = c.table_name
                  AND i.indisprimary
                  AND a.attname = c.column_name
            ) AS is_primary_key
        FROM information_schema.columns c
        WHERE c.table_schema = current_schema()
          AND c.table_name = $1
        ORDER BY c.ordinal_position
        "#;

    let rows = engine
        .fetch_all_sql_unprepared_with_binds(sql, &[EngineBindValue::String(table.to_string())])
        .await
        .map_err(|e| introspection_error("information_schema.columns", table, e))?;

    Ok(rows
        .iter()
        .filter_map(|row| {
            let name = row_string(row, "column_name")?;
            Some(LiveColumn {
                name,
                declared_type: row_string(row, "data_type").unwrap_or_default(),
                is_nullable: row_bool(row, "is_nullable"),
                is_primary_key: row_bool(row, "is_primary_key"),
                char_max_len: row_opt_i64(row, "char_max_len"),
                is_enum_udt: row_bool(row, "is_enum_udt"),
            })
        })
        .collect())
}

/// Live SQLite indexes that cover `column` on `table` (any position, including
/// composite indexes). Used by the destructive-drop path: explicit indexes
/// (`origin == "c"`) must be dropped before `DROP COLUMN`; constraint
/// autoindexes cannot be, and their presence makes the drop impossible.
pub async fn sqlite_indexes_covering_column(
    engine: &EngineHandle,
    table: &str,
    column: &str,
) -> PyResult<Vec<SqliteIndex>> {
    let list_sql = format!("PRAGMA index_list({})", quote_ident(table));
    let index_rows = engine
        .fetch_all_sql_unprepared(&list_sql)
        .await
        .map_err(|e| introspection_error("PRAGMA index_list", table, e))?;

    let mut covering = Vec::new();
    for index_row in &index_rows {
        let Some(index_name) = row_string(index_row, "name") else {
            continue;
        };
        let origin = row_string(index_row, "origin").unwrap_or_default();

        let info_sql = format!("PRAGMA index_info({})", quote_ident(&index_name));
        let column_rows = engine
            .fetch_all_sql_unprepared(&info_sql)
            .await
            .map_err(|e| introspection_error("PRAGMA index_info", table, e))?;
        let covers = column_rows
            .iter()
            .any(|row| row_string(row, "name").as_deref() == Some(column));
        if covers {
            covering.push(SqliteIndex {
                name: index_name,
                origin,
            });
        }
    }
    Ok(covering)
}

/// Live standalone indexes Ferro owns on `table`, normalized across backends.
pub async fn live_table_indexes(engine: &EngineHandle, table: &str) -> PyResult<Vec<LiveIndex>> {
    match engine.backend() {
        Dialect::Sqlite => sqlite_table_indexes(engine, table).await,
        Dialect::Postgres => postgres_table_indexes(engine, table).await,
    }
}

async fn sqlite_table_indexes(engine: &EngineHandle, table: &str) -> PyResult<Vec<LiveIndex>> {
    let list_sql = format!("PRAGMA index_list({})", quote_ident(table));
    let index_rows = engine
        .fetch_all_sql_unprepared(&list_sql)
        .await
        .map_err(|e| introspection_error("PRAGMA index_list", table, e))?;

    let mut out = Vec::new();
    for index_row in &index_rows {
        let Some(name) = row_string(index_row, "name") else { continue };
        if !is_ferro_index_name(&name) {
            continue;
        }
        let unique = row_bool(index_row, "unique");
        let info_sql = format!("PRAGMA index_info({})", quote_ident(&name));
        let col_rows = engine
            .fetch_all_sql_unprepared(&info_sql)
            .await
            .map_err(|e| introspection_error("PRAGMA index_info", table, e))?;
        // PRAGMA index_info returns rows in `seqno` order already.
        let columns: Vec<String> = col_rows.iter().filter_map(|r| row_string(r, "name")).collect();
        out.push(LiveIndex { name, columns, unique });
    }
    Ok(out)
}

async fn postgres_table_indexes(engine: &EngineHandle, table: &str) -> PyResult<Vec<LiveIndex>> {
    // One row per (index, column); `pos` orders columns within the index.
    let sql = r#"
        SELECT cl.relname::text AS index_name,
               i.indisunique     AS is_unique,
               a.attname::text   AS column_name,
               array_position(i.indkey::smallint[], a.attnum) AS pos
        FROM pg_index i
        JOIN pg_class cl ON cl.oid = i.indexrelid
        JOIN pg_class t  ON t.oid  = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
        WHERE n.nspname = current_schema()
          AND t.relname = $1
          AND NOT i.indisprimary
        ORDER BY cl.relname, pos
        "#;
    let rows = engine
        .fetch_all_sql_unprepared_with_binds(sql, &[EngineBindValue::String(table.to_string())])
        .await
        .map_err(|e| introspection_error("pg_index", table, e))?;

    // Group ordered rows into indexes (rows are already ordered by name, pos).
    let mut out: Vec<LiveIndex> = Vec::new();
    for row in &rows {
        let Some(name) = row_string(row, "index_name") else { continue };
        if !is_ferro_index_name(&name) {
            continue;
        }
        let unique = row_bool(row, "is_unique");
        let Some(column) = row_string(row, "column_name") else { continue };
        match out.last_mut() {
            Some(last) if last.name == name => last.columns.push(column),
            _ => out.push(LiveIndex { name, columns: vec![column], unique }),
        }
    }
    Ok(out)
}

/// Read every named CHECK constraint on `table`, ferro-owned or user-owned.
pub async fn live_table_checks(
    engine: &EngineHandle,
    table: &str,
) -> PyResult<Vec<LiveCheck>> {
    match engine.backend() {
        Dialect::Sqlite => sqlite_table_checks(engine, table).await,
        Dialect::Postgres => postgres_table_checks(engine, table).await,
    }
}

async fn sqlite_table_checks(engine: &EngineHandle, table: &str) -> PyResult<Vec<LiveCheck>> {
    let sql = format!(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = {}",
        quote_ident(table)
    );
    let rows = engine
        .fetch_all_sql_unprepared(&sql)
        .await
        .map_err(|e| introspection_error("sqlite_master", table, e))?;
    let Some(create_sql) = rows.first().and_then(|row| row_string(row, "sql")) else {
        return Ok(Vec::new());
    };
    Ok(parse_sqlite_inline_named_checks(&create_sql)
        .into_iter()
        .map(|(name, definition)| LiveCheck {
            ferro_owned: is_ferro_check_name(&name),
            name,
            definition,
        })
        .collect())
}

async fn postgres_table_checks(engine: &EngineHandle, table: &str) -> PyResult<Vec<LiveCheck>> {
    let sql = r#"
        SELECT con.conname::text AS name,
               pg_get_constraintdef(con.oid)::text AS definition
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE con.contype = 'c'
          AND nsp.nspname = current_schema()
          AND rel.relname = $1
        ORDER BY con.conname
        "#;
    let rows = engine
        .fetch_all_sql_unprepared_with_binds(sql, &[EngineBindValue::String(table.to_string())])
        .await
        .map_err(|e| introspection_error("pg_constraint", table, e))?;

    Ok(rows
        .iter()
        .filter_map(|row| {
            let name = row_string(row, "name")?;
            let definition = row_string(row, "definition")?;
            Some(LiveCheck {
                ferro_owned: is_ferro_check_name(&name),
                name,
                definition,
            })
        })
        .collect())
}

/// Parse `CONSTRAINT <name> CHECK (...)` clauses from a SQLite `CREATE TABLE`
/// statement stored in `sqlite_master.sql`.
pub(crate) fn parse_sqlite_inline_named_checks(create_sql: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let bytes = create_sql.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        let Some(rel) = find_ascii_case_insensitive(bytes, i, b"CONSTRAINT") else {
            break;
        };
        i = rel + b"CONSTRAINT".len();
        i = skip_ascii_whitespace(bytes, i);
        let Some(name) = parse_sql_identifier(bytes, &mut i) else {
            continue;
        };
        i = skip_ascii_whitespace(bytes, i);
        if !starts_ascii_case_insensitive(bytes, i, b"CHECK") {
            continue;
        }
        let check_start = i;
        i += b"CHECK".len();
        i = skip_ascii_whitespace(bytes, i);
        if i >= bytes.len() || bytes[i] != b'(' {
            continue;
        }
        let Some(end) = matching_close_paren(bytes, i) else {
            continue;
        };
        let definition = create_sql[check_start..=end].to_string();
        out.push((name, definition));
        i = end + 1;
    }
    out
}

fn find_ascii_case_insensitive(haystack: &[u8], start: usize, needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || start >= haystack.len() {
        return None;
    }
    haystack[start..]
        .windows(needle.len())
        .position(|window| window.eq_ignore_ascii_case(needle))
        .map(|pos| start + pos)
}

fn starts_ascii_case_insensitive(haystack: &[u8], start: usize, needle: &[u8]) -> bool {
    haystack
        .get(start..start + needle.len())
        .is_some_and(|window| window.eq_ignore_ascii_case(needle))
}

fn skip_ascii_whitespace(haystack: &[u8], mut i: usize) -> usize {
    while i < haystack.len() && haystack[i].is_ascii_whitespace() {
        i += 1;
    }
    i
}

fn parse_sql_identifier(haystack: &[u8], i: &mut usize) -> Option<String> {
    if *i >= haystack.len() {
        return None;
    }
    if haystack[*i] == b'"' {
        let start = *i + 1;
        let mut end = start;
        while end < haystack.len() {
            if haystack[end] == b'"' {
                if haystack.get(end + 1) == Some(&b'"') {
                    end += 2;
                    continue;
                }
                let name = String::from_utf8_lossy(&haystack[start..end]).replace("\"\"", "\"");
                *i = end + 1;
                return Some(name);
            }
            end += 1;
        }
        return None;
    }
    if !haystack[*i].is_ascii_alphabetic() && haystack[*i] != b'_' {
        return None;
    }
    let start = *i;
    *i += 1;
    while *i < haystack.len() && (haystack[*i].is_ascii_alphanumeric() || haystack[*i] == b'_') {
        *i += 1;
    }
    Some(String::from_utf8_lossy(&haystack[start..*i]).to_string())
}

fn matching_close_paren(haystack: &[u8], open: usize) -> Option<usize> {
    if haystack.get(open) != Some(&b'(') {
        return None;
    }
    let mut depth = 0i32;
    let mut in_single = false;
    let mut in_double = false;
    let mut escape = false;
    for (offset, &ch) in haystack[open..].iter().enumerate() {
        if in_single {
            if escape {
                escape = false;
            } else if ch == b'\'' {
                if haystack.get(open + offset + 1) == Some(&b'\'') {
                    escape = true;
                } else {
                    in_single = false;
                }
            }
            continue;
        }
        if in_double {
            if ch == b'"' {
                if haystack.get(open + offset + 1) == Some(&b'"') {
                    continue;
                }
                in_double = false;
            }
            continue;
        }
        match ch {
            b'\'' => in_single = true,
            b'"' => in_double = true,
            b'(' => depth += 1,
            b')' => {
                depth -= 1;
                if depth == 0 {
                    return Some(open + offset);
                }
            }
            _ => {}
        }
    }
    None
}

/// Test-only: read live CHECK constraints on `table` from the connected engine.
///
/// Returns a list of dicts with keys `name`, `definition`, and `ferro_owned`.
#[pyfunction]
#[pyo3(name = "_live_table_checks_for_test")]
#[pyo3(signature = (table, using=None))]
pub fn _live_table_checks_for_test(
    py: Python<'_>,
    table: String,
    using: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        let checks = live_table_checks(&engine, &table).await?;
        Python::attach(|py| {
            let out = pyo3::types::PyList::empty(py);
            for check in checks {
                let row = pyo3::types::PyDict::new(py);
                row.set_item("name", check.name)?;
                row.set_item("definition", check.definition)?;
                row.set_item("ferro_owned", check.ferro_owned)?;
                out.append(row)?;
            }
            Ok(out.into_any().unbind())
        })
    })
}


/// Read a live table's whole row-security state: the two `pg_class` flags and
/// every policy on it, ferro-owned or not (#413).
///
/// SQLite has no row-level security, so it reports the "off, no policies"
/// state — reconciliation then plans nothing there (ADR-0014), and the
/// create-pass warning remains the only thing said about the declaration.
pub async fn live_table_row_security(
    engine: &EngineHandle,
    table: &str,
) -> PyResult<LiveRowSecurity> {
    if engine.backend() != Dialect::Postgres {
        return Ok(LiveRowSecurity::default());
    }
    let flags_sql = r#"
        SELECT rel.relrowsecurity AS enabled,
               rel.relforcerowsecurity AS forced
        FROM pg_class rel
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = current_schema()
          AND rel.relname = $1
        "#;
    let flag_rows = engine
        .fetch_all_sql_unprepared_with_binds(flags_sql, &[EngineBindValue::String(table.to_string())])
        .await
        .map_err(|e| introspection_error("pg_class", table, e))?;
    let Some(flags) = flag_rows.first() else {
        // The table is gone — the caller's own "vanished between passes"
        // handling applies; report the empty state rather than guessing.
        return Ok(LiveRowSecurity::default());
    };

    let policies_sql = r#"
        SELECT pol.polname::text AS name,
               pol.polcmd::text AS cmd,
               pol.polpermissive AS permissive,
               pg_get_expr(pol.polqual, pol.polrelid)::text AS using_expr,
               pg_get_expr(pol.polwithcheck, pol.polrelid)::text AS with_check_expr
        FROM pg_policy pol
        JOIN pg_class rel ON rel.oid = pol.polrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = current_schema()
          AND rel.relname = $1
        ORDER BY pol.polname
        "#;
    let policy_rows = engine
        .fetch_all_sql_unprepared_with_binds(
            policies_sql,
            &[EngineBindValue::String(table.to_string())],
        )
        .await
        .map_err(|e| introspection_error("pg_policy", table, e))?;

    let mut policies = Vec::new();
    for row in &policy_rows {
        let Some(name) = row_string(row, "name") else {
            continue;
        };
        let code = row_string(row, "cmd").unwrap_or_default();
        // An unknown `polcmd` would be a Postgres version teaching policies a
        // command ferro has never heard of. Reporting it verbatim keeps the
        // drift decision honest: it will not match any declared command, so
        // the policy reads as drifted rather than as an accidental match.
        let command = row_policy_command_from_catalog_code(&code)
            .map(str::to_string)
            .unwrap_or(code);
        policies.push(LiveRowPolicy {
            ferro_owned: is_ferro_row_policy_name(&name),
            name,
            command,
            restrictive: !row_bool(row, "permissive"),
            using: row_string(row, "using_expr"),
            with_check: row_string(row, "with_check_expr"),
        });
    }

    Ok(LiveRowSecurity {
        enabled: row_bool(flags, "enabled"),
        forced: row_bool(flags, "forced"),
        policies,
    })
}

/// Whether the connected role is exempt from row-level security — a superuser
/// or a `BYPASSRLS` role (#413).
///
/// This is the question behind the migrator warning: a role that is *not*
/// exempt is filtered by the very policies the migration is installing, so its
/// own backfill can see zero rows and report success.
pub async fn connected_role_bypasses_row_security(engine: &EngineHandle) -> PyResult<bool> {
    if engine.backend() != Dialect::Postgres {
        return Ok(true);
    }
    let sql = "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user";
    let rows = engine
        .fetch_all_sql_unprepared(sql)
        .await
        .map_err(|e| introspection_error("pg_roles", "*", e))?;
    let Some(row) = rows.first() else {
        return Ok(false);
    };
    Ok(row_bool(row, "rolsuper") || row_bool(row, "rolbypassrls"))
}

/// Test-only: read `table`'s live row-security state from the connected engine.
///
/// Returns a dict with `enabled`, `forced`, and `policies` (each a dict of
/// `name`, `command`, `restrictive`, `using`, `with_check`, `ferro_owned`).
#[pyfunction]
#[pyo3(name = "_live_row_security_for_test")]
#[pyo3(signature = (table, using=None))]
pub fn _live_row_security_for_test(
    py: Python<'_>,
    table: String,
    using: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        let live = live_table_row_security(&engine, &table).await?;
        Python::attach(|py| {
            let out = pyo3::types::PyDict::new(py);
            out.set_item("enabled", live.enabled)?;
            out.set_item("forced", live.forced)?;
            let policies = pyo3::types::PyList::empty(py);
            for policy in live.policies {
                let row = pyo3::types::PyDict::new(py);
                row.set_item("name", policy.name)?;
                row.set_item("command", policy.command)?;
                row.set_item("restrictive", policy.restrictive)?;
                row.set_item("using", policy.using)?;
                row.set_item("with_check", policy.with_check)?;
                row.set_item("ferro_owned", policy.ferro_owned)?;
                policies.append(row)?;
            }
            out.set_item("policies", policies)?;
            Ok(out.into_any().unbind())
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::PoolSpec;
    use ferro_ddl_lowering::Dialect;

    async fn memory_engine() -> EngineHandle {
        EngineHandle::connect(PoolSpec {
            backend: Dialect::Sqlite,
            url: "sqlite::memory:".to_string(),
            search_path: None,
            max_connections: 1,
            min_connections: 0,
        })
        .await
        .unwrap()
    }

    #[test]
    fn ferro_check_names_are_recognized() {
        assert!(is_ferro_check_name("ck_transfer_at_most_one_outflow"));
        assert!(is_ferro_check_name("ck_doc_format"));
        assert!(!is_ferro_check_name("user_positive"));
        assert!(!is_ferro_check_name("my_custom_check"));
    }

    #[test]
    fn parse_sqlite_inline_named_checks_finds_ferro_and_user_owned() {
        let create_sql = "CREATE TABLE transfer (\
            id INTEGER PRIMARY KEY, \
            outflow_transaction_id INTEGER, \
            outflow_activity_id INTEGER, \
            amount REAL NOT NULL, \
            CONSTRAINT \"ck_transfer_at_most_one_outflow\" \
              CHECK ((\"outflow_transaction_id\" IS NULL) OR (\"outflow_activity_id\" IS NULL)), \
            CONSTRAINT \"user_positive\" CHECK (amount > 0)\
        )";
        let checks = parse_sqlite_inline_named_checks(create_sql);
        assert_eq!(checks.len(), 2);
        assert_eq!(checks[0].0, "ck_transfer_at_most_one_outflow");
        assert_eq!(
            checks[0].1,
            "CHECK ((\"outflow_transaction_id\" IS NULL) OR (\"outflow_activity_id\" IS NULL))"
        );
        assert_eq!(checks[1].0, "user_positive");
        assert_eq!(checks[1].1, "CHECK (amount > 0)");
    }

    #[tokio::test]
    async fn live_table_checks_reads_sqlite_inline_named_checks() {
        let engine = memory_engine().await;
        engine
            .execute_sql(
                "CREATE TABLE transfer (\
                 id INTEGER PRIMARY KEY, \
                 outflow_transaction_id INTEGER, \
                 outflow_activity_id INTEGER, \
                 amount REAL NOT NULL, \
                 CONSTRAINT \"ck_transfer_at_most_one_outflow\" \
                   CHECK ((\"outflow_transaction_id\" IS NULL) OR (\"outflow_activity_id\" IS NULL)), \
                 CONSTRAINT \"user_positive\" CHECK (amount > 0))",
            )
            .await
            .unwrap();

        let checks = live_table_checks(&engine, "transfer").await.unwrap();
        assert_eq!(checks.len(), 2);

        let ferro = checks
            .iter()
            .find(|c| c.name == "ck_transfer_at_most_one_outflow")
            .expect("ferro-owned check");
        assert!(ferro.ferro_owned);
        assert_eq!(
            ferro.definition,
            "CHECK ((\"outflow_transaction_id\" IS NULL) OR (\"outflow_activity_id\" IS NULL))"
        );

        let user = checks
            .iter()
            .find(|c| c.name == "user_positive")
            .expect("user-owned check");
        assert!(!user.ferro_owned);
        assert_eq!(user.definition, "CHECK (amount > 0)");
    }

    #[tokio::test]
    async fn live_table_checks_skips_unnamed_sqlite_inline_checks() {
        // SQLite allows column/table CHECK (...) without CONSTRAINT name, but
        // Ferro table checks are always named (ADR-0014). Unnamed fragments
        // have no stable identity for reconciliation — we do not surface them.
        let engine = memory_engine().await;
        engine
            .execute_sql(
                "CREATE TABLE item (id INTEGER PRIMARY KEY, amount REAL CHECK (amount > 0))",
            )
            .await
            .unwrap();

        let checks = live_table_checks(&engine, "item").await.unwrap();
        assert!(
            checks.is_empty(),
            "unnamed inline CHECK must not be surfaced: {checks:?}"
        );
    }

    #[test]
    fn ferro_index_names_are_recognized() {
        assert!(is_ferro_index_name("idx_user_email"));
        assert!(is_ferro_index_name("uq_user_email"));
        assert!(!is_ferro_index_name("sqlite_autoindex_user_1"));
        assert!(!is_ferro_index_name("user_email_key"));
        assert!(!is_ferro_index_name("my_custom_index"));
    }

    #[test]
    fn ferro_fk_names_are_recognized() {
        use ferro_ddl_lowering::is_ferro_fk_name;
        assert!(is_ferro_fk_name("fk_account_connection_id_connection"));
        assert!(!is_ferro_fk_name("account_connection_id_fkey"));
        assert!(!is_ferro_fk_name("my_custom_fk"));
    }

    #[test]
    fn confdeltype_maps_to_declared_action_vocabulary() {
        assert_eq!(fk_action_from_confdeltype("a"), Some("NO ACTION"));
        assert_eq!(fk_action_from_confdeltype("r"), Some("RESTRICT"));
        assert_eq!(fk_action_from_confdeltype("c"), Some("CASCADE"));
        assert_eq!(fk_action_from_confdeltype("n"), Some("SET NULL"));
        assert_eq!(fk_action_from_confdeltype("d"), Some("SET DEFAULT"));
        assert_eq!(fk_action_from_confdeltype("x"), None);
    }

    #[tokio::test]
    async fn live_table_names_lists_sqlite_tables() {
        let engine = memory_engine().await;
        engine
            .execute_sql("CREATE TABLE alpha (id integer)")
            .await
            .unwrap();
        engine
            .execute_sql("CREATE TABLE beta (id integer)")
            .await
            .unwrap();

        let names = live_table_names(&engine).await.unwrap();
        assert!(names.contains("alpha"));
        assert!(names.contains("beta"));
        assert!(!names.contains("gamma"));
    }

    #[tokio::test]
    async fn live_table_foreign_keys_reads_single_column_fks_and_skips_composite() {
        let engine = memory_engine().await;
        engine
            .execute_sql("CREATE TABLE connection (id INTEGER PRIMARY KEY)")
            .await
            .unwrap();
        engine
            .execute_sql("CREATE TABLE pair (a integer, b integer, PRIMARY KEY (a, b))")
            .await
            .unwrap();
        engine
            .execute_sql(
                "CREATE TABLE account (\
                 id INTEGER PRIMARY KEY, \
                 connection_id integer, \
                 pa integer, pb integer, \
                 CONSTRAINT fk_account_connection_id_connection \
                   FOREIGN KEY (connection_id) REFERENCES connection (id) \
                   ON DELETE SET NULL, \
                 FOREIGN KEY (pa, pb) REFERENCES pair (a, b))",
            )
            .await
            .unwrap();

        let fks = live_table_foreign_keys(&engine, "account").await.unwrap();
        assert_eq!(fks.len(), 1, "composite FK must be skipped: {fks:?}");
        let fk = &fks[0];
        // PRAGMA foreign_key_list exposes no constraint names.
        assert_eq!(fk.name, None);
        assert_eq!(fk.column, "connection_id");
        assert_eq!(fk.to_table, "connection");
        assert_eq!(fk.to_column, "id");
        assert_eq!(fk.on_delete, "SET NULL");
    }

    #[tokio::test]
    async fn live_table_columns_reads_sqlite_structure() {
        let engine = memory_engine().await;
        engine
            .execute_sql(
                "CREATE TABLE invoice (\
                 id INTEGER PRIMARY KEY AUTOINCREMENT, \
                 number varchar NOT NULL, \
                 paid_date date_text, \
                 total real NOT NULL DEFAULT 0)",
            )
            .await
            .unwrap();

        let columns = live_table_columns(&engine, "invoice")
            .await
            .unwrap()
            .expect("table exists");
        assert_eq!(columns.len(), 4);

        let by_name = |name: &str| columns.iter().find(|c| c.name == name).unwrap();
        let id = by_name("id");
        assert!(id.is_primary_key);
        let number = by_name("number");
        assert!(!number.is_nullable);
        assert_eq!(number.declared_type.to_lowercase(), "varchar");
        let paid_date = by_name("paid_date");
        assert!(paid_date.is_nullable);
        assert_eq!(paid_date.declared_type.to_lowercase(), "date_text");
        let total = by_name("total");
        assert!(!total.is_nullable);
    }

    #[tokio::test]
    async fn live_table_columns_returns_none_for_missing_table() {
        let engine = memory_engine().await;
        assert!(
            live_table_columns(&engine, "no_such_table")
                .await
                .unwrap()
                .is_none()
        );
    }

    #[tokio::test]
    async fn sqlite_indexes_covering_column_distinguishes_origin() {
        let engine = memory_engine().await;
        engine
            .execute_sql(
                "CREATE TABLE doc (id INTEGER PRIMARY KEY, slug TEXT UNIQUE, status TEXT, kind TEXT)",
            )
            .await
            .unwrap();
        engine
            .execute_sql("CREATE INDEX idx_doc_status ON doc (status)")
            .await
            .unwrap();
        engine
            .execute_sql("CREATE INDEX idx_doc_status_kind ON doc (status, kind)")
            .await
            .unwrap();

        let status_indexes = sqlite_indexes_covering_column(&engine, "doc", "status")
            .await
            .unwrap();
        let mut names: Vec<_> = status_indexes.iter().map(|i| i.name.as_str()).collect();
        names.sort_unstable();
        assert_eq!(names, ["idx_doc_status", "idx_doc_status_kind"]);
        assert!(status_indexes.iter().all(|i| i.origin == "c"));

        let slug_indexes = sqlite_indexes_covering_column(&engine, "doc", "slug")
            .await
            .unwrap();
        assert_eq!(slug_indexes.len(), 1);
        assert_eq!(slug_indexes[0].origin, "u");

        assert!(
            sqlite_indexes_covering_column(&engine, "doc", "id")
                .await
                .unwrap()
                .is_empty(),
            "rowid-alias PK has no autoindex"
        );
    }
}
