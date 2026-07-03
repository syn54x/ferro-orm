//! Shared schema-driven bind helpers for INSERT/UPDATE and query-filter paths.

use sea_query::{Alias, Expr, SimpleExpr};
use std::collections::HashMap;

/// Native Postgres enum UDT for `col_name`, from catalog introspection only.
///
/// Both bind paths use this so auto-migrate TEXT columns (which carry
/// `enum_type_name` in schema but are not `typtype = 'e'`) keep plain text
/// binds, while Alembic-created native enums cast even when the model
/// declares plain `str`.
pub(crate) fn native_postgres_enum_udt_name<'a>(
    col_name: &str,
    enum_udt: &'a HashMap<String, String>,
) -> Option<&'a str> {
    enum_udt.get(col_name).map(|s| s.as_str())
}

/// RHS expression for a non-null string compared against a native Postgres enum column.
pub(crate) fn postgres_enum_string_rhs_expr(s: &str, enum_type_name: &str) -> SimpleExpr {
    Expr::value(sea_query::Value::String(Some(Box::new(s.to_string()))))
        .cast_as(Alias::new(enum_type_name))
}
