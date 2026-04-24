use sqlx::{Any, Pool};
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
    pool: Arc<Pool<Any>>,
}

impl EngineHandle {
    pub fn new(backend: BackendKind, pool: Pool<Any>) -> Self {
        Self {
            backend,
            pool: Arc::new(pool),
        }
    }

    pub fn backend(&self) -> BackendKind {
        self.backend
    }

    pub fn pool(&self) -> Arc<Pool<Any>> {
        self.pool.clone()
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
}
