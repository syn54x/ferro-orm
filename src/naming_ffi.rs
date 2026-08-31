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

/// The check-addition decision over FFI (ADR-0013): given one model's compiled
/// SchemaIR and the CHECK constraint names its live table already carries,
/// return the Rust-rendered Postgres `ADD` statements (in declared order —
/// table checks, then column checks) and the constraint names they add.
///
/// The Alembic autogenerate comparator consumes this instead of re-deriving the
/// diff or re-rendering the SQL (AGENTS.md § I-1): the generated revision and
/// the auto-migrate reconciliation pass execute byte-identical statements.
/// Postgres-only, like the reconciliation pass itself (ADR-0014).
#[pyfunction]
pub fn _plan_check_addition(
    table: String,
    model_ir_json: String,
    live_names: Vec<String>,
) -> PyResult<String> {
    let model: ferro_schema_ir::SchemaModel =
        serde_json::from_str(&model_ir_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid SchemaIR model: {e}"))
        })?;
    let names = ferro_ddl_lowering::missing_check_names(&model, &live_names);
    let mut statements = Vec::with_capacity(names.len());
    for name in &names {
        let emission =
            ferro_ddl_lowering::render_check_addition(&table, &model, name, Dialect::Postgres)
                .ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "CHECK constraint '{name}' is missing from table '{table}' but has no \
                         declared artifact in the model IR"
                    ))
                })?;
        if let Some(statement) = emission.statement {
            statements.push(statement);
        }
    }
    Ok(serde_json::json!({ "statements": statements, "names": names }).to_string())
}

/// The check-rebuild decision over FFI (ADR-0015): given one model's compiled
/// SchemaIR and the live CHECK names + catalog definitions, return the
/// Rust-rendered Postgres `DROP` + bare `ADD` statements (in declared order)
/// and the constraint names they rebuild.
///
/// The Alembic autogenerate comparator consumes this instead of re-deriving
/// the diff or re-rendering the SQL (AGENTS.md § I-1). Postgres-only
/// (ADR-0014). There is no `migrate_updates` gate — running autogenerate is
/// itself the request for a diff.
#[pyfunction]
pub fn _plan_check_rebuild(
    table: String,
    model_ir_json: String,
    live: Vec<(String, String)>,
) -> PyResult<String> {
    let model: ferro_schema_ir::SchemaModel =
        serde_json::from_str(&model_ir_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid SchemaIR model: {e}"))
        })?;
    let names = ferro_ddl_lowering::drifted_check_names(&model, &live);
    let mut statements = Vec::with_capacity(names.len().saturating_mul(2));
    for name in &names {
        let emission =
            ferro_ddl_lowering::render_check_rebuild(&table, &model, name, Dialect::Postgres)
                .ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "CHECK constraint '{name}' drifted on table '{table}' but has no \
                         declared artifact in the model IR"
                    ))
                })?;
        statements.extend(emission.statements);
    }
    Ok(serde_json::json!({ "statements": statements, "names": names }).to_string())
}

/// The leftover-CHECK drop decision over FFI (ADR-0013): given one model's
/// compiled SchemaIR and the live ferro-owned CHECK names, return the
/// Rust-rendered Postgres `DROP CONSTRAINT` statements (in live order) and
/// the names they drop.
///
/// The Alembic autogenerate comparator consumes this instead of re-deriving
/// the diff or re-rendering the SQL (AGENTS.md § I-1). Postgres-only
/// (ADR-0014). There is no `migrate_destructive` gate — running autogenerate
/// is itself the request for a diff; the destructive flag is connect-time
/// safety only.
#[pyfunction]
pub fn _plan_check_drop(
    table: String,
    model_ir_json: String,
    live_ferro_owned_names: Vec<String>,
) -> PyResult<String> {
    let model: ferro_schema_ir::SchemaModel =
        serde_json::from_str(&model_ir_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid SchemaIR model: {e}"))
        })?;
    let declared: Vec<String> = model
        .table_checks
        .iter()
        .map(|check| check.name.clone())
        .chain(model.checks.iter().map(|check| check.name.clone()))
        .collect();
    let names = ferro_ddl_lowering::extra_check_names(&declared, &live_ferro_owned_names);
    let mut statements = Vec::with_capacity(names.len());
    for name in &names {
        let emission = ferro_ddl_lowering::render_check_drop(&table, name, Dialect::Postgres);
        if let Some(statement) = emission.statement {
            statements.push(statement);
        }
    }
    Ok(serde_json::json!({ "statements": statements, "names": names }).to_string())
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

/// Canonical row-policy name (`rls_<table>_<name>`) — the shared builder the
/// IR compiler stamps onto every `SchemaRowPolicy.name`, so no emitter ever
/// re-derives it (AGENTS.md § I-1).
#[pyfunction]
pub fn _ddl_row_policy_name(table: String, name: String) -> String {
    ferro_ddl_lowering::row_policy_name(&table, &name)
}

/// The column/setting shorthand's cast decision for one IR column, as a JSON
/// object: `{"supported": true, "cast": "uuid" | null}` for a column the
/// shorthand can render, `{"supported": false, "reason": "..."}` otherwise.
///
/// The IR compiler calls this at class-definition time so an unsupported column
/// type fails where the model is written, and the emitters call
/// `row_policy_shorthand_cast` for the same decision at render time — one
/// function, two doors (AGENTS.md § I-1).
#[pyfunction]
pub fn _rls_shorthand_cast(column_ir_json: String) -> PyResult<String> {
    let col: ferro_schema_ir::SchemaColumn =
        serde_json::from_str(&column_ir_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid SchemaIR column: {e}"))
        })?;
    let payload = match ferro_ddl_lowering::row_policy_shorthand_cast(&col) {
        Ok(cast) => serde_json::json!({ "supported": true, "cast": cast }),
        Err(reason) => serde_json::json!({ "supported": false, "reason": reason }),
    };
    Ok(payload.to_string())
}

/// The row-security create decision over FFI (PRD #406): given one model's
/// compiled SchemaIR, return the Rust-rendered Postgres statements a freshly
/// created table needs — `ENABLE`, `FORCE` when declared, then one
/// `CREATE POLICY` per policy in declaration order — plus the policy names.
///
/// This is the seam the Alembic autogenerate operation (#414) consumes so its
/// generated revision executes byte-identical SQL to the auto-migrate create
/// pass; neither side re-derives the diff or re-renders the SQL. Postgres-only
/// (ADR-0014): on SQLite the same function returns no statements and one
/// warning naming the table.
#[pyfunction]
#[pyo3(signature = (model_ir_json, dialect="postgres".to_string()))]
pub fn _plan_row_security(model_ir_json: String, dialect: String) -> PyResult<String> {
    let dialect = match dialect.as_str() {
        "postgres" => Dialect::Postgres,
        "sqlite" => Dialect::Sqlite,
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Unknown dialect {other:?}; expected 'postgres' or 'sqlite'"
            )));
        }
    };
    let model: ferro_schema_ir::SchemaModel =
        serde_json::from_str(&model_ir_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid SchemaIR model: {e}"))
        })?;
    let emission = ferro_ddl_lowering::row_security_statements(&model, dialect)
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    let names: Vec<String> = model
        .row_security
        .as_ref()
        .map(|declaration| {
            declaration
                .policies
                .iter()
                .map(|policy| policy.name.clone())
                .collect()
        })
        .unwrap_or_default();
    Ok(serde_json::json!({
        "statements": emission.statements,
        "names": names,
        "warning": emission.warning,
    })
    .to_string())
}
