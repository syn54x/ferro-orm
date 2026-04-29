use sqlx::ColumnIndex;
use sqlx::pool::PoolConnection;
use sqlx::{Column, PgPool, Postgres, Row, Sqlite, SqlitePool};
use std::fmt;
use std::sync::Arc;

/// Ferro's currently supported runtime database backends.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum BackendKind {
    #[default]
    Sqlite,
    Postgres,
}

impl BackendKind {
    /// Classify the backend from a connection URL.
    pub fn from_url(url: &str) -> Result<Self, UnsupportedDatabaseUrl> {
        if url.starts_with("sqlite:") {
            Ok(Self::Sqlite)
        } else if url.starts_with("postgres://") || url.starts_with("postgresql://") {
            Ok(Self::Postgres)
        } else {
            Err(UnsupportedDatabaseUrl::from_url(url))
        }
    }
}

/// Persistent runtime engine state for the currently connected backend.
#[derive(Clone, Debug)]
pub struct EngineHandle {
    backend: BackendKind,
    pool: BackendPool,
}

#[derive(Clone, Debug)]
enum BackendPool {
    Sqlite(Arc<SqlitePool>),
    Postgres(Arc<PgPool>),
}

#[allow(dead_code)]
pub enum EngineConnection {
    Sqlite(PoolConnection<Sqlite>),
    Postgres(PoolConnection<Postgres>),
}

/// Type tag carried by `EngineBindValue::Null` so the bind layer can emit a
/// type-correct `NULL` parameter on backends that perform strict OID
/// validation (notably PostgreSQL).
///
/// Schema-driven emission paths (INSERT/UPDATE values, query-filter
/// predicates, M2M target IDs) infer the kind from column metadata and emit
/// the matching variant. The raw-SQL bind path has no schema context and
/// emits `Untyped` — see `docs/solutions/patterns/typed-null-binds.md`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NullKind {
    Bool,
    I64,
    F64,
    String,
    Bytes,
    Uuid,
    /// Used by the raw-SQL bind path and as the documented fallback for any
    /// SeaQuery `Value` variant not yet mapped in `engine_bind_values_from_sea`.
    Untyped,
}

#[derive(Clone, Debug, PartialEq)]
pub enum EngineBindValue {
    Bool(bool),
    I64(i64),
    F64(f64),
    String(String),
    Bytes(Vec<u8>),
    /// A `NULL` bind tagged with its intended SQL type. See [`NullKind`].
    Null(NullKind),
}

#[derive(Clone, Debug, PartialEq)]
pub struct EngineRow {
    pub values: Vec<(String, EngineValue)>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EngineExecuteResult {
    pub rows_affected: u64,
    pub last_insert_id: Option<i64>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum EngineValue {
    Bool(bool),
    I64(i64),
    F64(f64),
    String(String),
    Bytes(Vec<u8>),
    Null,
}

impl EngineValue {
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Self::I64(value) => Some(*value),
            Self::String(value) => value.parse().ok(),
            _ => None,
        }
    }
}

impl EngineHandle {
    pub fn new_sqlite(pool: SqlitePool) -> Self {
        Self {
            backend: BackendKind::Sqlite,
            pool: BackendPool::Sqlite(Arc::new(pool)),
        }
    }

    pub fn new_postgres(pool: PgPool) -> Self {
        Self {
            backend: BackendKind::Postgres,
            pool: BackendPool::Postgres(Arc::new(pool)),
        }
    }

    pub fn backend(&self) -> BackendKind {
        self.backend
    }

    #[allow(dead_code)]
    pub fn sqlite_pool(&self) -> Option<Arc<SqlitePool>> {
        match &self.pool {
            BackendPool::Sqlite(pool) => Some(pool.clone()),
            BackendPool::Postgres(_) => None,
        }
    }

    #[allow(dead_code)]
    pub fn postgres_pool(&self) -> Option<Arc<PgPool>> {
        match &self.pool {
            BackendPool::Postgres(pool) => Some(pool.clone()),
            BackendPool::Sqlite(_) => None,
        }
    }

    pub async fn execute_sql(&self, sql: &str) -> Result<u64, sqlx::Error> {
        match &self.pool {
            BackendPool::Sqlite(pool) => {
                let result = sqlx::query(sql).execute(pool.as_ref()).await?;
                Ok(result.rows_affected())
            }
            BackendPool::Postgres(pool) => {
                let result = sqlx::query(sql).execute(pool.as_ref()).await?;
                Ok(result.rows_affected())
            }
        }
    }

    pub async fn execute_sql_with_binds(
        &self,
        sql: &str,
        values: &[EngineBindValue],
    ) -> Result<u64, sqlx::Error> {
        Ok(self
            .execute_sql_with_binds_result(sql, values)
            .await?
            .rows_affected)
    }

    pub async fn execute_sql_with_binds_result(
        &self,
        sql: &str,
        values: &[EngineBindValue],
    ) -> Result<EngineExecuteResult, sqlx::Error> {
        match &self.pool {
            BackendPool::Sqlite(pool) => {
                let mut query = sqlx::query(sql);
                for value in values {
                    query = bind_engine_value(query, value);
                }
                let result = query.execute(pool.as_ref()).await?;
                Ok(EngineExecuteResult {
                    rows_affected: result.rows_affected(),
                    last_insert_id: Some(result.last_insert_rowid()),
                })
            }
            BackendPool::Postgres(pool) => {
                let mut query = sqlx::query(sql);
                for value in values {
                    query = bind_engine_value(query, value);
                }
                let result = query.execute(pool.as_ref()).await?;
                Ok(EngineExecuteResult {
                    rows_affected: result.rows_affected(),
                    last_insert_id: None,
                })
            }
        }
    }

    pub async fn fetch_all_sql_with_binds(
        &self,
        sql: &str,
        values: &[EngineBindValue],
    ) -> Result<Vec<EngineRow>, sqlx::Error> {
        match &self.pool {
            BackendPool::Sqlite(pool) => {
                let mut query = sqlx::query(sql);
                for value in values {
                    query = bind_engine_value(query, value);
                }
                let rows = query.fetch_all(pool.as_ref()).await?;
                Ok(rows.iter().map(materialize_engine_row).collect())
            }
            BackendPool::Postgres(pool) => {
                let mut query = sqlx::query(sql);
                for value in values {
                    query = bind_engine_value(query, value);
                }
                let rows = query.fetch_all(pool.as_ref()).await?;
                Ok(rows.iter().map(materialize_engine_row).collect())
            }
        }
    }

    #[allow(dead_code)]
    pub async fn begin_transaction_connection(&self) -> Result<EngineConnection, sqlx::Error> {
        match &self.pool {
            BackendPool::Sqlite(pool) => {
                let mut conn = pool.acquire().await?;
                sqlx::query("BEGIN").execute(&mut *conn).await?;
                Ok(EngineConnection::Sqlite(conn))
            }
            BackendPool::Postgres(pool) => {
                let mut conn = pool.acquire().await?;
                sqlx::query("BEGIN").execute(&mut *conn).await?;
                Ok(EngineConnection::Postgres(conn))
            }
        }
    }
}

#[allow(dead_code)]
impl EngineConnection {
    pub async fn execute_sql(&mut self, sql: &str) -> Result<u64, sqlx::Error> {
        self.execute_sql_with_binds(sql, &[]).await
    }

    pub async fn execute_sql_with_binds(
        &mut self,
        sql: &str,
        values: &[EngineBindValue],
    ) -> Result<u64, sqlx::Error> {
        Ok(self
            .execute_sql_with_binds_result(sql, values)
            .await?
            .rows_affected)
    }

    pub async fn execute_sql_with_binds_result(
        &mut self,
        sql: &str,
        values: &[EngineBindValue],
    ) -> Result<EngineExecuteResult, sqlx::Error> {
        match self {
            EngineConnection::Sqlite(conn) => {
                let mut query = sqlx::query(sql);
                for value in values {
                    query = bind_engine_value(query, value);
                }
                let result = query.execute(&mut **conn).await?;
                Ok(EngineExecuteResult {
                    rows_affected: result.rows_affected(),
                    last_insert_id: Some(result.last_insert_rowid()),
                })
            }
            EngineConnection::Postgres(conn) => {
                let mut query = sqlx::query(sql);
                for value in values {
                    query = bind_engine_value(query, value);
                }
                let result = query.execute(&mut **conn).await?;
                Ok(EngineExecuteResult {
                    rows_affected: result.rows_affected(),
                    last_insert_id: None,
                })
            }
        }
    }

    pub async fn fetch_all_sql_with_binds(
        &mut self,
        sql: &str,
        values: &[EngineBindValue],
    ) -> Result<Vec<EngineRow>, sqlx::Error> {
        match self {
            EngineConnection::Sqlite(conn) => {
                let mut query = sqlx::query(sql);
                for value in values {
                    query = bind_engine_value(query, value);
                }
                let rows = query.fetch_all(&mut **conn).await?;
                Ok(rows.iter().map(materialize_engine_row).collect())
            }
            EngineConnection::Postgres(conn) => {
                let mut query = sqlx::query(sql);
                for value in values {
                    query = bind_engine_value(query, value);
                }
                let rows = query.fetch_all(&mut **conn).await?;
                Ok(rows.iter().map(materialize_engine_row).collect())
            }
        }
    }

    pub async fn commit(&mut self) -> Result<(), sqlx::Error> {
        self.execute_sql("COMMIT").await?;
        Ok(())
    }

    pub async fn rollback(&mut self) -> Result<(), sqlx::Error> {
        self.execute_sql("ROLLBACK").await?;
        Ok(())
    }
}

fn materialize_engine_row<R>(row: &R) -> EngineRow
where
    R: Row,
    for<'r> i32: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    for<'r> i64: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    for<'r> f64: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    for<'r> Vec<u8>: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    for<'r> String: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    for<'r> bool: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    usize: ColumnIndex<R>,
{
    let values = row
        .columns()
        .iter()
        .map(|column| {
            let name = column.name().to_string();
            let value = if let Ok(value) = row.try_get::<i64, _>(column.ordinal()) {
                EngineValue::I64(value)
            } else if let Ok(value) = row.try_get::<i32, _>(column.ordinal()) {
                EngineValue::I64(i64::from(value))
            } else if let Ok(value) = row.try_get::<f64, _>(column.ordinal()) {
                EngineValue::F64(value)
            } else if let Ok(value) = row.try_get::<String, _>(column.ordinal()) {
                EngineValue::String(value)
            } else if let Ok(value) = row.try_get::<Vec<u8>, _>(column.ordinal()) {
                EngineValue::Bytes(value)
            } else if let Ok(value) = row.try_get::<bool, _>(column.ordinal()) {
                EngineValue::Bool(value)
            } else {
                EngineValue::Null
            };
            (name, value)
        })
        .collect();

    EngineRow { values }
}

fn bind_engine_value<'q, DB>(
    query: sqlx::query::Query<'q, DB, <DB as sqlx::Database>::Arguments<'q>>,
    value: &'q EngineBindValue,
) -> sqlx::query::Query<'q, DB, <DB as sqlx::Database>::Arguments<'q>>
where
    DB: sqlx::Database,
    bool: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    f64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    String: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    Vec<u8>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    sqlx::types::Uuid: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    Option<bool>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    Option<f64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    Option<String>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    Option<Vec<u8>>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    Option<sqlx::types::Uuid>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
{
    match value {
        EngineBindValue::Bool(v) => query.bind(*v),
        EngineBindValue::I64(v) => query.bind(*v),
        EngineBindValue::F64(v) => query.bind(*v),
        EngineBindValue::String(v) => query.bind(v.clone()),
        EngineBindValue::Bytes(v) => query.bind(v.clone()),
        EngineBindValue::Null(NullKind::Bool) => query.bind(Option::<bool>::None),
        EngineBindValue::Null(NullKind::I64) => query.bind(Option::<i64>::None),
        EngineBindValue::Null(NullKind::F64) => query.bind(Option::<f64>::None),
        EngineBindValue::Null(NullKind::String) => query.bind(Option::<String>::None),
        EngineBindValue::Null(NullKind::Bytes) => query.bind(Option::<Vec<u8>>::None),
        EngineBindValue::Null(NullKind::Uuid) => query.bind(Option::<sqlx::types::Uuid>::None),
        // Raw-SQL path has no schema context; legacy text-typed null preserves
        // pre-refactor behavior. Schema-driven paths never construct this.
        EngineBindValue::Null(NullKind::Untyped) => query.bind(Option::<String>::None),
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UnsupportedDatabaseUrl {
    scheme: String,
}

impl UnsupportedDatabaseUrl {
    fn from_url(url: &str) -> Self {
        let scheme = url.split(':').next().unwrap_or_default().to_string();
        Self { scheme }
    }
}

impl fmt::Display for UnsupportedDatabaseUrl {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "Unsupported database URL scheme '{}'. Supported schemes: sqlite, postgres, postgresql",
            self.scheme
        )
    }
}

#[cfg(test)]
mod tests {
    use super::BackendKind;
    use super::EngineBindValue;
    use super::EngineHandle;
    use super::EngineValue;
    use sqlx::postgres::PgPoolOptions;
    use sqlx::sqlite::SqlitePoolOptions;

    #[test]
    fn classifies_sqlite_urls() {
        assert_eq!(
            BackendKind::from_url("sqlite::memory:").unwrap(),
            BackendKind::Sqlite
        );
        assert_eq!(
            BackendKind::from_url("sqlite:test.db?mode=rwc").unwrap(),
            BackendKind::Sqlite
        );
    }

    #[test]
    fn classifies_postgres_urls() {
        assert_eq!(
            BackendKind::from_url("postgres://user:pass@localhost/db").unwrap(),
            BackendKind::Postgres
        );
        assert_eq!(
            BackendKind::from_url("postgresql://user:pass@localhost/db").unwrap(),
            BackendKind::Postgres
        );
    }

    #[test]
    fn rejects_unsupported_schemes() {
        let error = BackendKind::from_url("mysql://user:pass@localhost/db").unwrap_err();
        assert_eq!(
            error.to_string(),
            "Unsupported database URL scheme 'mysql'. Supported schemes: sqlite, postgres, postgresql"
        );
    }

    #[tokio::test]
    async fn engine_handle_preserves_typed_sqlite_pool() {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();

        let engine = EngineHandle::new_sqlite(pool);

        assert_eq!(engine.backend(), BackendKind::Sqlite);
        assert!(engine.sqlite_pool().is_some());
        assert!(engine.postgres_pool().is_none());
    }

    #[tokio::test]
    async fn engine_handle_preserves_typed_postgres_pool() {
        let pool = PgPoolOptions::new()
            .max_connections(1)
            .connect_lazy("postgresql://example.invalid/postgres")
            .unwrap();

        let engine = EngineHandle::new_postgres(pool);

        assert_eq!(engine.backend(), BackendKind::Postgres);
        assert!(engine.sqlite_pool().is_none());
        assert!(engine.postgres_pool().is_some());
    }

    #[tokio::test]
    async fn engine_handle_executes_sqlite_sql_without_legacy_pool() {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let engine = EngineHandle::new_sqlite(pool);

        engine
            .execute_sql("CREATE TABLE typed_exec_check (id integer primary key)")
            .await
            .unwrap();
        engine
            .execute_sql("INSERT INTO typed_exec_check (id) VALUES (1)")
            .await
            .unwrap();

        assert_eq!(
            engine
                .execute_sql("UPDATE typed_exec_check SET id = 2")
                .await
                .unwrap(),
            1
        );
    }

    #[tokio::test]
    async fn engine_handle_executes_sqlite_sql_with_bound_values() {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let engine = EngineHandle::new_sqlite(pool);

        engine
            .execute_sql("CREATE TABLE typed_bind_check (id integer primary key, name text)")
            .await
            .unwrap();

        let inserted = engine
            .execute_sql_with_binds(
                "INSERT INTO typed_bind_check (id, name) VALUES (?, ?)",
                &[
                    EngineBindValue::I64(7),
                    EngineBindValue::String("ferro".to_string()),
                ],
            )
            .await
            .unwrap();

        assert_eq!(inserted, 1);
    }

    #[tokio::test]
    async fn engine_handle_fetches_sqlite_rows_with_bound_values() {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let engine = EngineHandle::new_sqlite(pool);

        engine
            .execute_sql("CREATE TABLE typed_fetch_check (id integer primary key, name text)")
            .await
            .unwrap();
        engine
            .execute_sql_with_binds(
                "INSERT INTO typed_fetch_check (id, name) VALUES (?, ?)",
                &[
                    EngineBindValue::I64(7),
                    EngineBindValue::String("ferro".to_string()),
                ],
            )
            .await
            .unwrap();

        let rows = engine
            .fetch_all_sql_with_binds(
                "SELECT id, name FROM typed_fetch_check WHERE id = ?",
                &[EngineBindValue::I64(7)],
            )
            .await
            .unwrap();

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].values[0], ("id".to_string(), EngineValue::I64(7)));
        assert_eq!(
            rows[0].values[1],
            ("name".to_string(), EngineValue::String("ferro".to_string()))
        );
    }

    #[tokio::test]
    async fn engine_handle_execute_result_includes_sqlite_last_insert_id() {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let engine = EngineHandle::new_sqlite(pool);

        engine
            .execute_sql("CREATE TABLE typed_insert_result (id integer primary key, name text)")
            .await
            .unwrap();

        let result = engine
            .execute_sql_with_binds_result(
                "INSERT INTO typed_insert_result (name) VALUES (?)",
                &[EngineBindValue::String("ferro".to_string())],
            )
            .await
            .unwrap();

        assert_eq!(result.rows_affected, 1);
        assert_eq!(result.last_insert_id, Some(1));
    }

    #[tokio::test]
    async fn engine_handle_commits_sqlite_transaction_connection() {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let engine = EngineHandle::new_sqlite(pool);

        engine
            .execute_sql("CREATE TABLE typed_tx_check (id integer primary key, name text)")
            .await
            .unwrap();

        let mut tx = engine.begin_transaction_connection().await.unwrap();
        tx.execute_sql_with_binds(
            "INSERT INTO typed_tx_check (name) VALUES (?)",
            &[EngineBindValue::String("ferro".to_string())],
        )
        .await
        .unwrap();
        tx.commit().await.unwrap();
        drop(tx);

        let rows = engine
            .fetch_all_sql_with_binds("SELECT name FROM typed_tx_check", &[])
            .await
            .unwrap();
        assert_eq!(
            rows[0].values[0],
            ("name".to_string(), EngineValue::String("ferro".to_string()))
        );
    }

    #[tokio::test]
    async fn engine_handle_rolls_back_sqlite_transaction_connection() {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let engine = EngineHandle::new_sqlite(pool);

        engine
            .execute_sql("CREATE TABLE typed_tx_rollback_check (id integer primary key, name text)")
            .await
            .unwrap();

        let mut tx = engine.begin_transaction_connection().await.unwrap();
        tx.execute_sql_with_binds(
            "INSERT INTO typed_tx_rollback_check (name) VALUES (?)",
            &[EngineBindValue::String("ferro".to_string())],
        )
        .await
        .unwrap();
        tx.rollback().await.unwrap();
        drop(tx);

        let rows = engine
            .fetch_all_sql_with_binds("SELECT name FROM typed_tx_rollback_check", &[])
            .await
            .unwrap();
        assert!(rows.is_empty());
    }

    #[test]
    fn engine_value_converts_integer_like_values_to_i64() {
        assert_eq!(EngineValue::I64(42).as_i64(), Some(42));
        assert_eq!(EngineValue::String("42".to_string()).as_i64(), Some(42));
        assert_eq!(EngineValue::Null.as_i64(), None);
    }

    #[test]
    fn null_kind_variants_are_distinct() {
        use super::NullKind;

        assert_ne!(NullKind::I64, NullKind::Bool);
        assert_ne!(NullKind::I64, NullKind::F64);
        assert_ne!(NullKind::I64, NullKind::String);
        assert_ne!(NullKind::I64, NullKind::Bytes);
        assert_ne!(NullKind::I64, NullKind::Uuid);
        assert_ne!(NullKind::I64, NullKind::Untyped);
        assert_ne!(NullKind::Untyped, NullKind::Bool);
        assert_ne!(NullKind::Untyped, NullKind::Uuid);
    }

    #[test]
    fn null_kind_equality_is_reflexive() {
        use super::NullKind;

        assert_eq!(NullKind::I64, NullKind::I64);
        assert_eq!(NullKind::Bool, NullKind::Bool);
        assert_eq!(NullKind::F64, NullKind::F64);
        assert_eq!(NullKind::String, NullKind::String);
        assert_eq!(NullKind::Bytes, NullKind::Bytes);
        assert_eq!(NullKind::Uuid, NullKind::Uuid);
        assert_eq!(NullKind::Untyped, NullKind::Untyped);
    }

    #[test]
    fn engine_bind_value_carries_null_kind() {
        use super::NullKind;

        let typed = EngineBindValue::Null(NullKind::I64);
        let untyped = EngineBindValue::Null(NullKind::Untyped);

        assert_eq!(typed, EngineBindValue::Null(NullKind::I64));
        assert_ne!(typed, untyped);
        assert_ne!(typed, EngineBindValue::Null(NullKind::Bool));
    }

    #[test]
    fn engine_bind_value_null_debug_output_includes_kind() {
        use super::NullKind;

        let typed = EngineBindValue::Null(NullKind::I64);
        let debug_repr = format!("{typed:?}");

        assert!(
            debug_repr.contains("Null"),
            "Debug should mention Null variant: {debug_repr}"
        );
        assert!(
            debug_repr.contains("I64"),
            "Debug should mention NullKind::I64: {debug_repr}"
        );
    }
}
