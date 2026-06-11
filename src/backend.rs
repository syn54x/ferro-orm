use sqlx::ColumnIndex;
use sqlx::pool::PoolConnection;
use sqlx::postgres::PgPoolOptions;
use sqlx::sqlite::SqlitePoolOptions;
use sqlx::{Column, Connection, PgPool, Postgres, Row, Sqlite, SqlitePool, ValueRef};
use std::fmt;
use std::sync::{Arc, RwLock};

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

/// Everything needed to (re)build a connection pool. Owned by `EngineHandle`
/// so the engine can atomically replace its pool after schema-changing DDL
/// (see [`EngineHandle::refresh_pool`]).
#[derive(Clone, Debug)]
pub struct PoolSpec {
    pub backend: BackendKind,
    pub url: String,
    /// Postgres `SET search_path` applied to every new connection
    /// (the `ferro_search_path` URL parameter).
    pub search_path: Option<String>,
    pub max_connections: u32,
    pub min_connections: u32,
}

impl PoolSpec {
    /// Build a fresh pool from this spec.
    async fn build(&self) -> Result<BackendPool, sqlx::Error> {
        match self.backend {
            BackendKind::Sqlite => {
                let pool = SqlitePoolOptions::new()
                    .max_connections(self.max_connections)
                    .min_connections(self.min_connections)
                    .connect(&self.url)
                    .await?;
                Ok(BackendPool::Sqlite(Arc::new(pool)))
            }
            BackendKind::Postgres => {
                let mut pool_options = PgPoolOptions::new()
                    .max_connections(self.max_connections)
                    .min_connections(self.min_connections);
                if let Some(search_path) = &self.search_path {
                    let set_search_path_sql =
                        Arc::new(format!("SET search_path TO {}", search_path));
                    pool_options = pool_options.after_connect(move |conn, _meta| {
                        let set_search_path_sql = set_search_path_sql.clone();
                        Box::pin(async move {
                            sqlx::query(set_search_path_sql.as_str())
                                .execute(conn)
                                .await?;
                            Ok(())
                        })
                    });
                }
                let pool = pool_options.connect(&self.url).await?;
                Ok(BackendPool::Postgres(Arc::new(pool)))
            }
        }
    }

    /// In-memory SQLite databases live inside their connections: replacing the
    /// pool would discard the database itself, so refresh must clear statement
    /// caches in place instead of rebuilding.
    fn is_ephemeral_sqlite(&self) -> bool {
        if self.backend != BackendKind::Sqlite {
            return false;
        }
        let url = self.url.to_ascii_lowercase();
        url.contains(":memory:") || url.contains("mode=memory")
    }
}

/// Persistent runtime engine state for the currently connected backend.
///
/// The pool lives behind a shared `RwLock` so [`EngineHandle::refresh_pool`]
/// can atomically swap in a fresh pool after schema-changing DDL; clones of a
/// handle observe the swap because they share the same slot.
#[derive(Clone, Debug)]
pub struct EngineHandle {
    backend: BackendKind,
    pool: Arc<RwLock<BackendPool>>,
    /// How to rebuild the pool. `None` for handles wrapped around an
    /// externally built pool (test-only constructors), which cannot refresh.
    spec: Option<PoolSpec>,
    /// When false, Ferro skips the identity map for this connection (no lookup/register on load).
    identity_map_enabled: bool,
}

#[derive(Clone, Debug)]
enum BackendPool {
    Sqlite(Arc<SqlitePool>),
    Postgres(Arc<PgPool>),
}

impl BackendPool {
    async fn close(&self) {
        match self {
            BackendPool::Sqlite(pool) => pool.close().await,
            BackendPool::Postgres(pool) => pool.close().await,
        }
    }
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
    /// Typed UUID bind. Required so non-null UUIDs reach Postgres with the
    /// correct OID instead of being coerced to `text` (which fails since PG
    /// has no implicit `text -> uuid` cast). See `engine_bind_values_from_sea`.
    Uuid(sqlx::types::Uuid),
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
    /// Connect a new engine from a [`PoolSpec`]. This is the canonical
    /// constructor: handles built this way know how to rebuild their pool and
    /// therefore support [`EngineHandle::refresh_pool`].
    pub async fn connect(spec: PoolSpec) -> Result<Self, sqlx::Error> {
        let backend = spec.backend;
        let pool = spec.build().await?;
        Ok(Self {
            backend,
            pool: Arc::new(RwLock::new(pool)),
            spec: Some(spec),
            identity_map_enabled: true,
        })
    }

    /// Wrap an externally built SQLite pool. The handle cannot rebuild the
    /// pool, so `refresh_pool` fails loudly. Test-only: production code must
    /// construct engines via [`EngineHandle::connect`].
    #[cfg(test)]
    pub fn new_sqlite(pool: SqlitePool) -> Self {
        Self {
            backend: BackendKind::Sqlite,
            pool: Arc::new(RwLock::new(BackendPool::Sqlite(Arc::new(pool)))),
            spec: None,
            identity_map_enabled: true,
        }
    }

    /// Wrap an externally built Postgres pool. The handle cannot rebuild the
    /// pool, so `refresh_pool` fails loudly. Test-only: production code must
    /// construct engines via [`EngineHandle::connect`].
    #[cfg(test)]
    pub fn new_postgres(pool: PgPool) -> Self {
        Self {
            backend: BackendKind::Postgres,
            pool: Arc::new(RwLock::new(BackendPool::Postgres(Arc::new(pool)))),
            spec: None,
            identity_map_enabled: true,
        }
    }

    /// Snapshot the current pool. Cheap: `sqlx::Pool` is a reference-counted
    /// handle. Poisoning is recovered rather than propagated — the pool value
    /// itself is always valid (writers only ever replace it wholesale) and
    /// panicking here would cross the FFI boundary (AGENTS.md § I-3).
    fn pool_snapshot(&self) -> BackendPool {
        match self.pool.read() {
            Ok(guard) => guard.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }

    /// Atomically swap in a freshly built pool and gracefully close the old
    /// one. After this returns, no future query can be served by a connection
    /// whose statement cache predates the swap — the engine-level guarantee
    /// that makes DDL on a live engine safe (sqlx's SQLite worker panics and
    /// silently returns zero rows on stale cached statements; Postgres raises
    /// "cached plan must not change result type").
    ///
    /// In-memory SQLite databases live inside their connections, so the pool
    /// cannot be replaced without losing the database. For those, every
    /// connection is acquired (waiting for outstanding work to drain) and its
    /// statement cache cleared in place — the same guarantee, data intact.
    pub async fn refresh_pool(&self) -> Result<(), sqlx::Error> {
        let Some(spec) = &self.spec else {
            return Err(sqlx::Error::Configuration(
                "this EngineHandle wraps an externally built pool and cannot rebuild it; \
                 construct it via EngineHandle::connect to enable refresh_pool"
                    .into(),
            ));
        };

        if spec.is_ephemeral_sqlite() {
            return self.clear_all_statement_caches(spec).await;
        }

        let new_pool = spec.build().await?;
        let old_pool = {
            let mut guard = match self.pool.write() {
                Ok(guard) => guard,
                Err(poisoned) => poisoned.into_inner(),
            };
            std::mem::replace(&mut *guard, new_pool)
        };
        old_pool.close().await;
        Ok(())
    }

    /// Acquire every pool connection and clear its statement cache. Holding
    /// all connections simultaneously guarantees none escapes the sweep;
    /// acquisition waits for checked-out connections to be returned, so
    /// outstanding work drains before the caches are cleared.
    async fn clear_all_statement_caches(&self, spec: &PoolSpec) -> Result<(), sqlx::Error> {
        match &self.pool_snapshot() {
            BackendPool::Sqlite(pool) => {
                let mut held = Vec::with_capacity(spec.max_connections as usize);
                for _ in 0..spec.max_connections {
                    held.push(pool.acquire().await?);
                }
                for conn in &mut held {
                    conn.clear_cached_statements().await?;
                }
            }
            BackendPool::Postgres(pool) => {
                let mut held = Vec::with_capacity(spec.max_connections as usize);
                for _ in 0..spec.max_connections {
                    held.push(pool.acquire().await?);
                }
                for conn in &mut held {
                    conn.clear_cached_statements().await?;
                }
            }
        }
        Ok(())
    }

    /// Returns whether this connection uses the identity map (singleton instances per PK).
    #[must_use]
    pub fn is_identity_map_enabled(&self) -> bool {
        self.identity_map_enabled
    }

    /// Sets identity-map behavior for this handle (used by `connect(identity_map=...)`).
    #[must_use]
    pub fn with_identity_map_enabled(mut self, enabled: bool) -> Self {
        self.identity_map_enabled = enabled;
        self
    }

    pub fn backend(&self) -> BackendKind {
        self.backend
    }

    #[allow(dead_code)]
    pub fn sqlite_pool(&self) -> Option<Arc<SqlitePool>> {
        match &self.pool_snapshot() {
            BackendPool::Sqlite(pool) => Some(pool.clone()),
            BackendPool::Postgres(_) => None,
        }
    }

    #[allow(dead_code)]
    pub fn postgres_pool(&self) -> Option<Arc<PgPool>> {
        match &self.pool_snapshot() {
            BackendPool::Postgres(pool) => Some(pool.clone()),
            BackendPool::Sqlite(_) => None,
        }
    }

    pub async fn execute_sql(&self, sql: &str) -> Result<u64, sqlx::Error> {
        match &self.pool_snapshot() {
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

    /// Execute without entering any connection's prepared-statement cache.
    /// Migration DDL and schema introspection must use this: caching a
    /// statement against a schema that the very same migration is about to
    /// change would poison the connection it ran on.
    pub async fn execute_sql_unprepared(&self, sql: &str) -> Result<u64, sqlx::Error> {
        match &self.pool_snapshot() {
            BackendPool::Sqlite(pool) => {
                let result = sqlx::query(sql)
                    .persistent(false)
                    .execute(pool.as_ref())
                    .await?;
                Ok(result.rows_affected())
            }
            BackendPool::Postgres(pool) => {
                let result = sqlx::query(sql)
                    .persistent(false)
                    .execute(pool.as_ref())
                    .await?;
                Ok(result.rows_affected())
            }
        }
    }

    /// Fetch without entering any connection's prepared-statement cache.
    /// See [`EngineHandle::execute_sql_unprepared`].
    pub async fn fetch_all_sql_unprepared(&self, sql: &str) -> Result<Vec<EngineRow>, sqlx::Error> {
        self.fetch_all_sql_unprepared_with_binds(sql, &[]).await
    }

    /// Bind-supporting variant of [`EngineHandle::fetch_all_sql_unprepared`].
    pub async fn fetch_all_sql_unprepared_with_binds(
        &self,
        sql: &str,
        values: &[EngineBindValue],
    ) -> Result<Vec<EngineRow>, sqlx::Error> {
        match &self.pool_snapshot() {
            BackendPool::Sqlite(pool) => {
                let mut query = sqlx::query(sql).persistent(false);
                for value in values {
                    query = bind_engine_value(query, value);
                }
                let rows = query.fetch_all(pool.as_ref()).await?;
                Ok(rows.iter().map(materialize_engine_row).collect())
            }
            BackendPool::Postgres(pool) => {
                let mut query = sqlx::query(sql).persistent(false);
                for value in values {
                    query = bind_engine_value(query, value);
                }
                let rows = query.fetch_all(pool.as_ref()).await?;
                Ok(rows.iter().map(materialize_engine_row).collect())
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
        match &self.pool_snapshot() {
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
        match &self.pool_snapshot() {
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
        match &self.pool_snapshot() {
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
            let ordinal = column.ordinal();
            // SQLite (and some drivers) let `try_get::<i64>` succeed with 0 on SQL
            // NULL when the column has INTEGER/NUMERIC affinity (including Alembic
            // `DATETIME`). Always consult the raw value before typed decode.
            let value = match row.try_get_raw(ordinal) {
                Ok(raw) if raw.is_null() => EngineValue::Null,
                Ok(_) | Err(_) => decode_non_null_engine_value(row, ordinal),
            };
            (name, value)
        })
        .collect();

    EngineRow { values }
}

fn decode_non_null_engine_value<R>(row: &R, ordinal: usize) -> EngineValue
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
    if let Ok(value) = row.try_get::<i64, _>(ordinal) {
        EngineValue::I64(value)
    } else if let Ok(value) = row.try_get::<i32, _>(ordinal) {
        EngineValue::I64(i64::from(value))
    } else if let Ok(value) = row.try_get::<f64, _>(ordinal) {
        EngineValue::F64(value)
    } else if let Ok(value) = row.try_get::<String, _>(ordinal) {
        EngineValue::String(value)
    } else if let Ok(value) = row.try_get::<Vec<u8>, _>(ordinal) {
        EngineValue::Bytes(value)
    } else if let Ok(value) = row.try_get::<bool, _>(ordinal) {
        EngineValue::Bool(value)
    } else {
        EngineValue::Null
    }
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
        EngineBindValue::Uuid(v) => query.bind(*v),
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
    use super::PoolSpec;
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
    async fn engine_handle_fetches_sqlite_null_columns_as_null_not_zero() {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let engine = EngineHandle::new_sqlite(pool);

        for (table, ddl) in [
            ("null_int", "CREATE TABLE null_int (v INTEGER)"),
            ("null_real", "CREATE TABLE null_real (v REAL)"),
            ("null_text", "CREATE TABLE null_text (v TEXT)"),
            ("null_datetime", "CREATE TABLE null_datetime (v DATETIME)"),
        ] {
            engine.execute_sql(ddl).await.unwrap();
            engine
                .execute_sql(&format!("INSERT INTO {table} DEFAULT VALUES"))
                .await
                .unwrap();
            let rows = engine
                .fetch_all_sql_with_binds(&format!("SELECT v FROM {table}"), &[])
                .await
                .unwrap();
            assert_eq!(
                rows[0].values[0].1,
                EngineValue::Null,
                "SQL NULL in {table} must not decode as integer zero"
            );
        }
    }

    #[tokio::test]
    async fn engine_handle_fetches_sqlite_non_null_zero_integer() {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let engine = EngineHandle::new_sqlite(pool);

        engine
            .execute_sql("CREATE TABLE zero_int (v INTEGER NOT NULL)")
            .await
            .unwrap();
        engine
            .execute_sql("INSERT INTO zero_int (v) VALUES (0)")
            .await
            .unwrap();

        let rows = engine
            .fetch_all_sql_with_binds("SELECT v FROM zero_int", &[])
            .await
            .unwrap();

        assert_eq!(rows[0].values[0].1, EngineValue::I64(0));
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

    fn file_backed_spec(path: &std::path::Path) -> PoolSpec {
        PoolSpec {
            backend: BackendKind::Sqlite,
            url: format!("sqlite://{}?mode=rwc", path.display()),
            search_path: None,
            max_connections: 2,
            min_connections: 0,
        }
    }

    #[tokio::test]
    async fn refresh_pool_survives_alter_table_on_file_backed_sqlite() {
        let dir = std::env::temp_dir().join(format!("ferro_refresh_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let db_path = dir.join("refresh_swap.db");
        let _ = std::fs::remove_file(&db_path);

        let engine = EngineHandle::connect(file_backed_spec(&db_path))
            .await
            .unwrap();
        engine
            .execute_sql("CREATE TABLE swap_check (id INTEGER PRIMARY KEY, a TEXT)")
            .await
            .unwrap();
        engine
            .execute_sql("INSERT INTO swap_check (id, a) VALUES (1, 'x')")
            .await
            .unwrap();

        // Prepare (and cache) a statement against the pre-DDL schema.
        let rows = engine
            .fetch_all_sql_with_binds("SELECT * FROM swap_check", &[])
            .await
            .unwrap();
        assert_eq!(rows.len(), 1);

        // Widen the table, then refresh. The same SELECT * must now see the
        // new column instead of panicking on a stale cached statement.
        engine
            .execute_sql_unprepared("ALTER TABLE swap_check ADD COLUMN b TEXT")
            .await
            .unwrap();
        engine.refresh_pool().await.unwrap();

        let rows = engine
            .fetch_all_sql_with_binds("SELECT * FROM swap_check", &[])
            .await
            .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(
            rows[0].values.len(),
            3,
            "post-refresh row must include the added column"
        );

        let _ = std::fs::remove_file(&db_path);
    }

    #[tokio::test]
    async fn refresh_pool_preserves_in_memory_database() {
        let spec = PoolSpec {
            backend: BackendKind::Sqlite,
            url: "sqlite::memory:".to_string(),
            search_path: None,
            max_connections: 1,
            min_connections: 0,
        };
        let engine = EngineHandle::connect(spec).await.unwrap();
        engine
            .execute_sql("CREATE TABLE mem_check (id INTEGER PRIMARY KEY)")
            .await
            .unwrap();
        engine
            .execute_sql("INSERT INTO mem_check (id) VALUES (1)")
            .await
            .unwrap();

        // Must clear statement caches in place rather than swapping the pool,
        // which would discard the in-memory database.
        engine.refresh_pool().await.unwrap();

        let rows = engine
            .fetch_all_sql_with_binds("SELECT id FROM mem_check", &[])
            .await
            .unwrap();
        assert_eq!(rows.len(), 1, "in-memory data must survive refresh_pool");
    }

    #[tokio::test]
    async fn refresh_pool_fails_loudly_for_externally_built_pools() {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let engine = EngineHandle::new_sqlite(pool);

        let err = engine.refresh_pool().await.unwrap_err();
        assert!(
            err.to_string().contains("externally built pool"),
            "unexpected error: {err}"
        );
    }
}
