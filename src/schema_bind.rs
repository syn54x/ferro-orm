//! Shared schema-driven bind helpers for INSERT/UPDATE and query-filter paths.

use sea_query::{Alias, Expr, SimpleExpr};
use std::collections::HashMap;

/// Resolve the Postgres enum UDT name for a column when binding a string RHS.
///
/// `enum_udt` comes from catalog introspection (INSERT/UPDATE). `col_info` is
/// the model field schema fragment (`enum_type_name`, `db_type`, etc.).
///
/// When `db_type` is set, native enum casting is suppressed — the column is no
/// longer stored as a Postgres enum UDT (see AGENTS.md I-1 / Alembic parity).
pub(crate) fn postgres_enum_type_name_for_column(
    col_name: &str,
    enum_udt: &HashMap<String, String>,
    col_info: Option<&serde_json::Value>,
) -> Option<String> {
    if let Some(info) = col_info
        && info.get("db_type").and_then(|v| v.as_str()).is_some()
    {
        return None;
    }

    enum_udt.get(col_name).cloned().or_else(|| {
        col_info?
            .get("enum_type_name")?
            .as_str()
            .map(std::string::ToString::to_string)
    })
}

/// RHS expression for a non-null string compared against a native Postgres enum column.
pub(crate) fn postgres_enum_string_rhs_expr(s: &str, enum_type_name: &str) -> SimpleExpr {
    Expr::value(sea_query::Value::String(Some(Box::new(s.to_string()))))
        .cast_as(Alias::new(enum_type_name))
}
