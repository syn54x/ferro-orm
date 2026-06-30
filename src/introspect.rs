//! Live database schema introspection.
//!
//! Reads the structure of existing tables so the auto-migrate diff
//! (`src/migrate.rs`) can compare them against registered model schemas.
//! All queries run unprepared — introspection precedes DDL, and neither may
//! populate a connection's statement cache (see `EngineHandle::execute_sql_unprepared`).

use crate::backend::{EngineBindValue, EngineHandle, EngineRow, EngineValue};
use crate::state::Dialect;
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
    pyo3::exceptions::PyRuntimeError::new_err(format!(
        "Schema introspection failed ({context} for table '{table}'): {err}"
    ))
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
    fn ferro_index_names_are_recognized() {
        assert!(is_ferro_index_name("idx_user_email"));
        assert!(is_ferro_index_name("uq_user_email"));
        assert!(!is_ferro_index_name("sqlite_autoindex_user_1"));
        assert!(!is_ferro_index_name("user_email_key"));
        assert!(!is_ferro_index_name("my_custom_index"));
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
