//! FFI surface for the single-sourced DDL decision tables: artifact-name
//! builders (FF-B B3) and derived-type storage resolution (FF-B B2).
//!
//! The Python IR compiler (`src/ferro/ir/compiler.py`) and the Alembic bridge
//! consume these instead of re-implementing the rules — name formats, their
//! 63-char truncation guards, and the `(logical_type, format, db_type, enum)`
//! → storage decision live only in `ferro-ddl-lowering` (AGENTS.md § I-1).

use ferro_ddl_lowering::{Dialect, ResolvedStorage};
use pyo3::prelude::*;

/// The names of the two tracking tables (`_ferro_migrations`,
/// `_ferro_migrations_format`): what the Alembic bridge's object filter hides
/// from Alembic's own comparator.
#[pyfunction]
pub fn _tracking_table_names() -> (&'static str, &'static str) {
    (crate::run::TRACKING_TABLE, crate::run::FORMAT_TABLE)
}

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

/// The type-provenance decision for one generated Alembic revision
/// (ADR-0020, ADR-0021, ADR-0022; AGENTS.md § I-1 item 17). `declaring_json`
/// maps each declared native enum type name to the `[table, column]` pairs
/// that declare it (the model's columns plus the columns the revision drops,
/// which its downgrade restores); `added_json` lists the `[table, column]`
/// pairs the revision adds; `inline_created_json` the subset of those on
/// tables the revision creates; `labels_json` maps each declared type name to
/// its labels in declaration order. Returns a JSON list of verdicts keyed by
/// type name, one per touched type:
///
/// - `introduced`: every declaring column is one the revision adds. The
///   downgrade must execute `drop_statement` (the Rust-rendered `DROP TYPE`)
///   after its last `drop_table` / `drop_column`. `create_statement` is the
///   Rust-rendered guarded `CREATE TYPE` the upgrade must execute ahead of
///   its table operations when no `create_table` of the revision creates the
///   type inline (`add_column` only, #439), else null.
/// - `reused`: an added column and a surviving one. The type already lives
///   wherever the revision can run: both statements are null, the revision's
///   `create_table` columns must render `create_type=False` (#443), and the
///   downgrade leaves it alone.
///
/// The Alembic autogenerate comparator consumes this instead of re-deriving
/// a verdict or re-rendering the SQL.
#[pyfunction]
pub fn _plan_enum_type_provenance(
    declaring_json: String,
    added_json: String,
    inline_created_json: String,
    labels_json: String,
) -> PyResult<String> {
    let declaring: std::collections::BTreeMap<String, Vec<(String, String)>> =
        serde_json::from_str(&declaring_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid enum declaring columns: {e}"))
        })?;
    let added: Vec<(String, String)> = serde_json::from_str(&added_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid enum added columns: {e}"))
    })?;
    let inline_created: Vec<(String, String)> = serde_json::from_str(&inline_created_json)
        .map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "Invalid enum inline-created columns: {e}"
            ))
        })?;
    let labels: std::collections::BTreeMap<String, Vec<String>> =
        serde_json::from_str(&labels_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid enum labels: {e}"))
        })?;
    let verdicts = ferro_ddl_lowering::enum_type_provenance(&declaring, &added, &inline_created)
        .into_iter()
        .map(|(name, provenance)| match provenance {
            ferro_ddl_lowering::EnumTypeProvenance::Introduced { creation } => {
                let create_statement = match creation {
                    ferro_ddl_lowering::EnumTypeCreation::Inline => None,
                    ferro_ddl_lowering::EnumTypeCreation::Statement => {
                        let type_labels = labels.get(&name).ok_or_else(|| {
                            pyo3::exceptions::PyValueError::new_err(format!(
                                "Enum type {name:?} is introduced by add_column alone but \
                                 its declared labels were not supplied"
                            ))
                        })?;
                        Some(ferro_ddl_lowering::render_pg_enum_create_type(
                            &name,
                            type_labels,
                        ))
                    }
                };
                Ok(serde_json::json!({
                    "name": name,
                    "provenance": "introduced",
                    "create_statement": create_statement,
                    "drop_statement": ferro_ddl_lowering::render_pg_enum_drop_type(&name),
                }))
            }
            ferro_ddl_lowering::EnumTypeProvenance::Reused => Ok(serde_json::json!({
                "name": name,
                "provenance": "reused",
                "create_statement": serde_json::Value::Null,
                "drop_statement": serde_json::Value::Null,
            })),
        })
        .collect::<PyResult<Vec<serde_json::Value>>>()?;
    Ok(serde_json::Value::Array(verdicts).to_string())
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

/// The row-policy command table over FFI: every command a policy may be scoped
/// to, and which clauses Postgres accepts for it, as a JSON array of
/// `{"command": "all", "using": true, "with_check": true}`.
///
/// The Python declaration surface (`ferro.rowsecurity`) reads this once at
/// import instead of keeping its own copy: the command allowlist and the
/// USING / WITH CHECK rules are decided in `ferro-ddl-lowering` alone, so a
/// command added there cannot drift out of the declaration's validation
/// (AGENTS.md § I-1).
#[pyfunction]
pub fn _rls_command_matrix() -> String {
    let rows: Vec<serde_json::Value> = ferro_ddl_lowering::ROW_POLICY_COMMANDS
        .iter()
        .map(|command| {
            serde_json::json!({
                "command": ferro_ddl_lowering::row_policy_command_token(*command),
                "using": ferro_ddl_lowering::row_policy_command_takes_using(*command),
                "with_check": ferro_ddl_lowering::row_policy_command_takes_with_check(*command),
            })
        })
        .collect();
    serde_json::Value::Array(rows).to_string()
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

/// One row-policy expression through ferro's normalizer, over FFI (#413).
///
/// Exposed so the drift comparison can be exercised — and pinned — against
/// REAL `pg_get_expr` output from a live database, which is the only way to
/// prove an unchanged declaration plans nothing.
#[pyfunction]
pub fn _normalize_row_policy_expr(expr: String) -> String {
    ferro_ddl_lowering::normalize_row_policy_expr(&expr)
}

/// Decode one `pg_policy.polcmd` catalog code into ferro's command vocabulary
/// (`"all"`, `"select"`, `"insert"`, `"update"`, `"delete"`), or `None` for a
/// code ferro does not recognize.
///
/// The Alembic autogenerate comparator introspects `pg_policy` directly (it
/// has no `EngineHandle` to call `live_table_row_security` through) and needs
/// this exact decode to build the `LiveRowPolicy` payload the planner's
/// live facts carry — the same table
/// `src/introspect.rs`'s `live_table_row_security` uses, over FFI, so the two
/// introspection paths cannot drift apart (AGENTS.md § I-1).
#[pyfunction]
pub fn _row_policy_command_from_catalog_code(code: String) -> Option<String> {
    ferro_ddl_lowering::row_policy_command_from_catalog_code(&code).map(str::to_string)
}

/// Whether a live policy name follows ferro's `rls_` ownership convention.
///
/// The Alembic autogenerate comparator's own `pg_policy` introspection needs
/// this to fill in `LiveRowPolicy.ferro_owned` — the same test
/// `src/introspect.rs`'s `live_table_row_security` applies, over FFI
/// (AGENTS.md § I-1).
#[pyfunction]
pub fn _is_ferro_row_policy_name(name: String) -> bool {
    ferro_ddl_lowering::is_ferro_row_policy_name(&name)
}
