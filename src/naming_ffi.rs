//! FFI surface for the single-sourced DDL decision tables: artifact-name
//! builders (FF-B B3) and derived-type storage resolution (FF-B B2).
//!
//! The Python IR compiler (`src/ferro/ir/compiler.py`) and the Alembic bridge
//! consume these instead of re-implementing the rules — name formats, their
//! 63-char truncation guards, and the `(logical_type, format, db_type, enum)`
//! → storage decision live only in `ferro-ddl-lowering` (AGENTS.md § I-1).

use ferro_ddl_lowering::{Dialect, ResolvedStorage};
use pyo3::prelude::*;

#[pyfunction]
pub fn _ddl_single_index_name(table: String, column: String) -> String {
    ferro_ddl_lowering::single_index_name(&table, &column)
}

#[pyfunction]
pub fn _ddl_single_unique_name(table: String, column: String) -> String {
    ferro_ddl_lowering::single_unique_index_name(&table, &column)
}

#[pyfunction]
pub fn _ddl_composite_index_name(table: String, columns: Vec<String>) -> String {
    let refs: Vec<&str> = columns.iter().map(String::as_str).collect();
    ferro_ddl_lowering::composite_index_name(&table, &refs)
}

#[pyfunction]
pub fn _ddl_composite_unique_name(table: String, columns: Vec<String>) -> String {
    let refs: Vec<&str> = columns.iter().map(String::as_str).collect();
    ferro_ddl_lowering::composite_unique_index_name(&table, &refs)
}

#[pyfunction]
pub fn _ddl_check_constraint_name(table: String, column: String) -> String {
    ferro_ddl_lowering::db_check_constraint_name(&table, &column)
}

#[pyfunction]
pub fn _ddl_table_check_constraint_name(table: String, suffix: String) -> String {
    ferro_ddl_lowering::table_check_constraint_name(&table, &suffix)
}

#[pyfunction]
pub fn _ddl_fk_name(table: String, column: String, to_table: String) -> String {
    ferro_ddl_lowering::fk_name(&table, &column, &to_table)
}

/// Resolve one IR column's storage decision. `column_ir_json` is a single
/// SchemaIR column object (as produced by `compile_schema_ir_payload`);
/// `dialect` is `"postgres"` or `"sqlite"`. Returns JSON:
/// `{"kind": "scalar", "token": "<db_type token>"}` or
/// `{"kind": "pg_enum", "name": "<type name>", "labels": ["...", ...]}`.
/// Unknown logical types raise `RuntimeError` — never a silent varchar fallback.
#[pyfunction]
pub fn _resolve_storage_type(column_ir_json: String, dialect: String) -> PyResult<String> {
    let dialect = match dialect.as_str() {
        "postgres" => Dialect::Postgres,
        "sqlite" => Dialect::Sqlite,
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Unknown dialect {:?}; expected 'postgres' or 'sqlite'",
                other
            )));
        }
    };
    let col: ferro_schema_ir::SchemaColumn =
        serde_json::from_str(&column_ir_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid SchemaIR column: {}", e))
        })?;
    let storage = ferro_ddl_lowering::resolve_column_storage(&col, dialect)
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    let payload = match storage {
        ResolvedStorage::Scalar(canonical) => serde_json::json!({
            "kind": "scalar",
            "token": ferro_ddl_lowering::canonical_to_db_type_token(canonical, dialect),
        }),
        ResolvedStorage::PgEnum { type_name, labels } => serde_json::json!({
            "kind": "pg_enum",
            "name": type_name,
            "labels": labels,
        }),
    };
    Ok(payload.to_string())
}

/// The label-addition decision over FFI (ADR-0011): given one enum type's
/// declared and live labels, return the Rust-rendered `ADD VALUE` statements
/// (in declared order) and the extra warn-never-act labels (in live order).
/// The Alembic autogenerate comparator consumes this instead of re-deriving
/// the diff or re-rendering the SQL (AGENTS.md § I-1) — the auto-migrate
/// planner and the generated revision execute byte-identical statements.
#[pyfunction]
pub fn _plan_enum_label_addition(
    type_name: String,
    declared: Vec<String>,
    live: Vec<String>,
) -> String {
    let statements: Vec<String> = ferro_ddl_lowering::missing_enum_labels(&declared, &live)
        .iter()
        .map(|label| ferro_ddl_lowering::render_pg_enum_add_value(&type_name, label))
        .collect();
    serde_json::json!({
        "statements": statements,
        "extra_labels": ferro_ddl_lowering::extra_enum_labels(&declared, &live),
    })
    .to_string()
}

/// Render the shared `db_check` CHECK body (`"col" IN (v1, v2, ...)`) —
/// byte-identical to the Rust emitters. `values` arrive pre-rendered (quoted)
/// from the IR compiler.
#[pyfunction]
pub fn _render_check_body(column: String, values: Vec<String>) -> String {
    ferro_ddl_lowering::render_check_body(&ferro_schema_ir::SchemaCheck {
        name: String::new(),
        column,
        values,
    })
}

/// Render a table-check CHECK body from a structured predicate JSON object
/// (the `predicate` field of `SchemaTableCheck`). Byte-identical to the Rust
/// emitters (I-1).
#[pyfunction]
pub fn _render_table_check_body(predicate_json: String) -> PyResult<String> {
    let predicate: ferro_schema_ir::CheckExpr =
        serde_json::from_str(&predicate_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid check predicate: {e}"))
        })?;
    Ok(ferro_ddl_lowering::render_check_expr(&predicate))
}
