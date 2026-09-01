//! Schema registration and table-creation orchestration.
//!
//! Model registration flows through the SchemaIR column slice; CREATE TABLE is
//! emitted from the Python-compiled SchemaIR modelset via `ferro_migrate`.

use crate::backend::EngineHandle;
use crate::state::{Dialect, MODEL_REGISTRY, engine_for_connection};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use pyo3::prelude::*;
use std::collections::HashMap;
use std::sync::Arc;

fn model_ir_dependencies(
    table_name: &str,
    modelset: &IrEnvelope<SchemaIrPayload>,
) -> Vec<String> {
    let Some(model) = modelset
        .payload
        .models
        .iter()
        .find(|m| m.table_name == table_name)
    else {
        return Vec::new();
    };
    let mut deps: Vec<String> = model
        .foreign_keys
        .iter()
        .map(|fk| fk.to_table.clone())
        .collect();
    deps.sort();
    deps.dedup();
    deps
}

/// Dependency-sort registered models for the migrate ALTER reconcile loop.
///
/// FK edges come from [`SchemaModel::foreign_keys`] in the pushed modelset.
/// Entries are sorted by model name first, then delegated to
/// [`ferro_migrate::order_by_dependencies`] — the shared primitive that also
/// orders CREATE TABLE emission — so self-referential FKs, external targets,
/// and cycle fallback behave identically on both paths (#302).
pub(crate) fn order_models_for_migration(
    registry: HashMap<String, Arc<crate::state::RegisteredModel>>,
    modelset: &IrEnvelope<SchemaIrPayload>,
) -> Vec<(String, Arc<crate::state::RegisteredModel>)> {
    let mut entries: Vec<(String, Arc<crate::state::RegisteredModel>)> =
        registry.into_iter().collect();
    entries.sort_by(|a, b| a.0.cmp(&b.0));

    ferro_migrate::order_by_dependencies(
        entries,
        |(_, model)| model.table_name.clone(),
        |(_, model)| model_ir_dependencies(&model.table_name, modelset),
    )
}

/// Internal utility to create all registered tables in the database.
///
/// This is used by both the `connect(auto_migrate=True)` flow and the
/// manual `create_tables()` function.
///
/// Returns the tables that already existed when the pass started — the set the
/// reconciliation pass owns (ADR-0010). A table this pass created is already
/// exactly the model, so re-diffing it would only replay the create pass's own
/// backend-limitation warnings.
///
/// # Errors
/// Returns a `PyErr` if the SQL execution fails.
pub async fn internal_create_tables(
    engine: Arc<EngineHandle>,
    reconciliation_follows: bool,
) -> PyResult<std::collections::HashSet<String>> {
    // The runtime CREATE TABLE path is emitted from the Python-compiled SchemaIR
    // via the shared `ferro_migrate` emitter (issue #153). The modelset must have
    // been pushed by the `connect`/`create_tables` Python wrappers first — a
    // missing modelset is a loud error, never a silent empty create.
    let modelset = {
        let guard = crate::state::SCHEMA_IR_MODELSET.read().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock SchemaIR modelset")
        })?;
        guard.clone().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(
                "SchemaIR modelset not set — connect()/create_tables() must push it before creating tables",
            )
        })?
    };

    let dialect = engine.backend();

    // ADR-0010: the create pass owns only missing tables. An existing table —
    // whatever its shape — belongs to the reconciliation pass; firing even
    // `IF NOT EXISTS` index DDL at it can reference columns only the
    // reconcile pass will add (#324).
    let existing_tables = crate::introspect::live_table_names(&engine).await?;

    let model_refs: Vec<&ferro_schema_ir::SchemaModel> =
        modelset.payload.models.iter().collect();
    for model in ferro_migrate::order_models_for_create(&model_refs) {
        if existing_tables.contains(&model.table_name) {
            crate::log_debug(format!(
                "Ferro Engine: Table '{}' already exists — left to the reconciliation pass",
                model.table_name
            ));
            // A declaration the create pass cannot act on must never pass in
            // silence: the author believes the table is policed. When the
            // reconciliation pass runs next it APPLIES the declaration to this
            // very table (#413), so warning here would cry wolf about a gap
            // that is about to be closed — the warning is for the connect that
            // does not reconcile (plain `auto_migrate=True`). On SQLite the
            // reconciliation pass emits no row-security DDL at all (ADR-0014),
            // so the warning always stands there.
            if (!reconciliation_follows || dialect != Dialect::Postgres)
                && let Some(warning) =
                    ferro_ddl_lowering::row_security_existing_table_warning(model, dialect)
            {
                crate::emit_user_warning_always(&warning);
            }
            continue;
        }
        let emission = ferro_migrate::render_create_table(model, dialect).map_err(|err| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "CREATE TABLE emission failed for '{}': {}",
                model.table_name, err.message
            ))
        })?;

        create_one_table(&engine, model, &emission, dialect).await?;

        for warning in &emission.warnings {
            crate::emit_user_warning(warning);
        }

        crate::log_debug(format!("✅ Ferro Engine: Table '{}' created", model.table_name));
    }

    Ok(existing_tables)
}

/// Execute one model's whole create emission — enum guards, `CREATE TABLE`, and
/// every post-create artifact (indexes, checks, row-security flags and
/// policies) — as ONE unit.
///
/// On Postgres this is a single transaction, mirroring the reconciliation
/// pass's per-table transaction (FF-G G3): a table ends fully created or not
/// created at all. That is not a nicety for row security, it is the whole
/// contract. `ENABLE`/`FORCE ROW LEVEL SECURITY` land before the policies, so a
/// `CREATE POLICY` that fails halfway would otherwise leave a live table with
/// row security forced on and **zero** policies — default-deny for every role
/// including the owner — that the create pass can never repair, because it only
/// ever touches missing tables (ADR-0010). Rolling the table back instead
/// leaves the database exactly as it was and the connect fails loudly, naming
/// the statement.
///
/// SQLite keeps statement-at-a-time execution, matching the reconciliation
/// pass; it emits no row-security DDL at all, so the lockout shape cannot occur
/// there.
async fn create_one_table(
    engine: &Arc<EngineHandle>,
    model: &ferro_schema_ir::SchemaModel,
    emission: &ferro_migrate::CreateTableEmission,
    dialect: Dialect,
) -> PyResult<()> {
    let table = model.table_name.as_str();
    if dialect != Dialect::Postgres {
        for pre_sql in &emission.pre_create_sqls {
            engine
                .execute_sql_unprepared(pre_sql)
                .await
                .map_err(|e| create_step_error(table, "enum type", pre_sql, e))?;
        }
        engine
            .execute_sql(&emission.create_sql)
            .await
            .map_err(|e| create_step_error(table, "table", &emission.create_sql, e))?;
        for post_sql in &emission.post_create_sqls {
            engine
                .execute_sql(post_sql)
                .await
                .map_err(|e| create_step_error(table, "artifact", post_sql, e))?;
        }
        return Ok(());
    }

    let mut conn = engine.begin_transaction_connection().await.map_err(|e| {
        crate::errors::map_db_error(
            &format!("Auto-migrate failed to open a transaction to create table '{table}'"),
            e,
        )
    })?;
    let table_result: PyResult<()> = async {
        for pre_sql in &emission.pre_create_sqls {
            conn.execute_sql_unprepared(pre_sql)
                .await
                .map_err(|e| create_step_error(table, "enum type", pre_sql, e))?;
        }
        conn.execute_sql(&emission.create_sql)
            .await
            .map_err(|e| create_step_error(table, "table", &emission.create_sql, e))?;
        for post_sql in &emission.post_create_sqls {
            conn.execute_sql(post_sql)
                .await
                .map_err(|e| create_step_error(table, "artifact", post_sql, e))?;
        }
        Ok(())
    }
    .await;

    match table_result {
        Ok(()) => conn.commit().await.map_err(|e| {
            crate::errors::map_db_error(
                &format!("Auto-migrate failed to commit the creation of table '{table}'"),
                e,
            )
        }),
        Err(err) => {
            // A connection whose ROLLBACK failed may be stuck
            // idle-in-transaction, and sqlx only pings on release — handing it
            // back to the pool would serve that state to the next checkout.
            // Same disposal `begin_transaction_with_settings` performs (#416).
            if let Err(rollback_err) = conn.rollback().await {
                crate::log_debug(format!(
                    "⚠️ Ferro Engine: rollback after failed creation of '{table}' also failed: \
                     {rollback_err} — discarding the connection"
                ));
                let _ = conn.detach_and_close().await;
            }
            Err(err)
        }
    }
}

/// One create-step failure, naming the table and the exact statement — the
/// difference between "connect failed" and "connect failed, here is the policy
/// you mistyped".
fn create_step_error(table: &str, step: &str, sql: &str, err: sqlx::Error) -> pyo3::PyErr {
    crate::errors::map_db_error(
        &format!("SQL Execution failed for '{table}' {step} (statement: {sql})"),
        err,
    )
}

/// Legacy per-model Rust registration from a SchemaIR column slice.
///
/// Class-body registration no longer calls this (#246); the bulk install seam
/// at `connect()`/`create_tables()`/`migrate()` is the sole population path.
/// Retained for direct test callers and backward-compatible FFI exposure.
///
/// # Errors
/// Returns a `PyErr` if the columns JSON is invalid or if the registry is locked.
#[pyfunction]
#[pyo3(signature = (name, columns, table_name))]
pub fn register_model_schema(
    name: String,
    columns: String,
    table_name: String,
) -> PyResult<()> {
    let parsed_columns: Vec<ferro_schema_ir::SchemaColumn> =
        serde_json::from_str(&columns).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid SchemaIR columns: {}", e))
        })?;
    if table_name.is_empty() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "table_name must be a non-empty string",
        ));
    }

    let registered = crate::state::RegisteredModel::new(parsed_columns, table_name)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;

    let mut registry = MODEL_REGISTRY
        .write()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry"))?;

    registry.insert(name.clone(), registered);
    crate::log_debug(format!("⚙️  Ferro Engine: Map generated for '{}'", name));
    Ok(())
}

/// Manually triggers table creation for all registered models.
///
/// Returns an awaitable object (Python coroutine).
///
/// # Errors
/// Returns a `PyErr` if the engine is not initialized or if SQL execution fails.
#[pyfunction]
#[pyo3(signature = (using=None))]
pub fn create_tables(py: Python<'_>, using: Option<String>) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = engine_for_connection(using)?;
        // `create_tables()` is the create pass on its own — nothing reconciles
        // an existing table afterwards, so a declaration on one is reported.
        internal_create_tables(engine, false)
            .await
            .map(|_existing| ())
    })
}

/// Test-only helper: render the Rust emitter's CREATE TABLE SQL plus any
/// post-create SQL fragments (CHECK constraints, composite indexes) without
/// requiring a live database. Used by the cross-emitter parity test (U5 of
/// the configurable-column-storage plan) to assert that the Rust and Alembic
/// emitters agree on every `(canonical_token, dialect)` pair.
///
/// `dialect` must be `"postgres"` or `"sqlite"`. Anything else raises
/// `ValueError`. `schema_json` is a SchemaIR *payload* JSON string of the shape
/// `{"dialect_agnostic": bool, "models": [<SchemaModel>...]}` produced by
/// `ferro.ir.compiler.compile_schema_ir_payload`. The model matching `name`
/// (by `model_name`/`table_name`, falling back to the first) is rendered through
/// the same `ferro_migrate::render_create_table` emitter the runtime uses.
///
/// # Errors
/// Returns a `PyErr` when the JSON cannot be parsed, the dialect is
/// unrecognized, the payload has no models, or the emitter fails.
#[pyfunction]
#[pyo3(name = "_render_create_table_sql_for_test")]
pub fn _render_create_table_sql_for_test(
    name: String,
    schema_json: String,
    dialect: String,
) -> PyResult<(String, Vec<String>, Vec<String>)> {
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
    let payload: ferro_schema_ir::SchemaIrPayload = serde_json::from_str(&schema_json)
        .map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid SchemaIR payload: {}", e))
        })?;
    let table_lower = name.to_lowercase();
    let model = payload
        .models
        .iter()
        .find(|m| m.model_name == name || m.table_name == table_lower)
        .or_else(|| payload.models.first())
        .ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "SchemaIR payload for {:?} contains no models",
                name
            ))
        })?;
    let emission = ferro_migrate::render_create_table(model, dialect).map_err(|err| {
        pyo3::exceptions::PyRuntimeError::new_err(format!(
            "CREATE TABLE emission failed for '{}': {}",
            model.table_name, err.message
        ))
    })?;
    Ok((
        emission.create_sql,
        emission.post_create_sqls,
        emission.pre_create_sqls,
    ))
}

/// Build minimal SchemaIR columns from a JSON schema fixture for Rust unit tests.
#[cfg(test)]
pub fn infer_test_schema_columns(schema: &serde_json::Value) -> Vec<ferro_schema_ir::SchemaColumn> {
    use ferro_schema_ir::SchemaColumn;

    let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) else {
        return Vec::new();
    };

    let mut columns = Vec::new();
    for (name, raw_col) in properties {
        let resolved = resolve_ref(schema, raw_col);
        let (json_type, format) = property_json_type_and_format(resolved);
        let db_type = raw_col
            .get("db_type")
            .or_else(|| resolved.get("db_type"))
            .and_then(|v| v.as_str());
        let enum_values = resolved
            .get("enum")
            .or_else(|| raw_col.get("enum"))
            .and_then(|v| v.as_array())
            .filter(|v| !v.is_empty())
            .cloned();
        let enum_type_name = raw_col
            .get("enum_type_name")
            .or_else(|| resolved.get("enum_type_name"))
            .and_then(|v| v.as_str());
        let logical_type = infer_test_logical_type(
            json_type,
            format,
            db_type,
            enum_values.as_ref(),
            enum_type_name,
        );
        let db_type_explicit = db_type.is_some_and(|token| !token.is_empty());
        let primary_key = column_bool_metadata(raw_col, resolved, "primary_key").unwrap_or(false);
        let autoincrement = if primary_key {
            column_bool_metadata(raw_col, resolved, "autoincrement").unwrap_or(true)
        } else {
            false
        };
        columns.push(SchemaColumn {
            name: name.clone(),
            logical_type,
            db_type: db_type.map(str::to_string),
            db_type_explicit: db_type_explicit.then_some(true),
            nullable: !primary_key,
            primary_key,
            autoincrement,
            unique: column_bool_metadata(raw_col, resolved, "unique").unwrap_or(false),
            index: column_bool_metadata(raw_col, resolved, "index").unwrap_or(false),
            default: None,
            format: format.map(str::to_string),
            enum_values,
            enum_type_name: enum_type_name.map(str::to_string),
            postgres_native_enum: false,
        });
    }
    columns
}

#[cfg(test)]
fn resolve_ref<'a>(
    schema: &'a serde_json::Value,
    col_info: &'a serde_json::Value,
) -> &'a serde_json::Value {
    if let Some(ref_path) = col_info.get("$ref").and_then(|r| r.as_str())
        && let Some(def_name) = ref_path.strip_prefix("#/$defs/")
        && let Some(def) = schema.get("$defs").and_then(|defs| defs.get(def_name))
    {
        return def;
    }
    col_info
}

#[cfg(test)]
fn property_json_type_and_format(
    col_info: &serde_json::Value,
) -> (Option<&str>, Option<&str>) {
    let top_type = col_info.get("type").and_then(|t| t.as_str());
    let top_format = col_info.get("format").and_then(|f| f.as_str());
    if top_type.is_some() {
        return (top_type, top_format);
    }

    if let Some(items) = col_info.get("anyOf").and_then(|a| a.as_array()) {
        for item in items {
            let item_type = item.get("type").and_then(|t| t.as_str());
            if item_type == Some("null") {
                continue;
            }
            let item_format = item.get("format").and_then(|f| f.as_str());
            return (item_type, item_format.or(top_format));
        }
    }

    (None, top_format)
}

#[cfg(test)]
fn json_schema_logical_type(json_type: &str, format: Option<&str>) -> &'static str {
    match (json_type, format) {
        ("integer", _) => "integer",
        ("number", Some("decimal")) => "decimal",
        ("number", _) => "number",
        ("boolean", _) => "boolean",
        ("string", Some("date-time")) => "datetime",
        ("string", Some("date")) => "date",
        ("string", Some("time")) => "time",
        ("string", Some("uuid")) => "uuid",
        ("string", Some("binary")) => "binary",
        ("string", _) => "string",
        ("object" | "array", _) => "json",
        _ => "unknown",
    }
}

#[cfg(test)]
fn column_bool_metadata(
    raw_col_info: &serde_json::Value,
    resolved_col_info: &serde_json::Value,
    key: &str,
) -> Option<bool> {
    raw_col_info
        .get(key)
        .or_else(|| resolved_col_info.get(key))
        .and_then(|value| value.as_bool())
}

#[cfg(test)]
fn infer_test_logical_type(
    json_type: Option<&str>,
    format: Option<&str>,
    db_type: Option<&str>,
    enum_values: Option<&Vec<serde_json::Value>>,
    enum_type_name: Option<&str>,
) -> String {
    let from_json = json_schema_logical_type(json_type.unwrap_or(""), format);
    if from_json != "unknown" {
        return from_json.to_string();
    }
    if enum_values.is_some() {
        return if json_type == Some("integer") {
            "integer".to_string()
        } else {
            "string".to_string()
        };
    }
    if enum_type_name.is_some() {
        return "string".to_string();
    }
    if let Some(token) = db_type.filter(|t| !t.is_empty()) {
        return match token {
            "bytea" => "binary",
            "uuid" => "uuid",
            "int" | "integer" | "bigint" | "smallint" => "integer",
            "boolean" => "boolean",
            "double" | "float" | "real" => "number",
            "numeric" | "decimal" => "decimal",
            "date" => "date",
            "time" => "time",
            "timestamp" | "timestamptz" | "datetime" => "datetime",
            "json" | "jsonb" => "json",
            _ => "string",
        }
        .to_string();
    }
    "string".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::RegisteredModel;
    use ferro_ddl_lowering::{composite_index_name, db_check_constraint_name};
    use ferro_schema_ir::{SchemaForeignKey, SchemaModel};
    use std::sync::Arc;

    fn test_registered(table_name: &str) -> Arc<RegisteredModel> {
        RegisteredModel::new(vec![], table_name.to_string()).expect("test registration")
    }

    fn test_modelset(models: Vec<SchemaModel>) -> IrEnvelope<SchemaIrPayload> {
        IrEnvelope {
            ir_kind: "schema".to_string(),
            ir_version: 1,
            payload: SchemaIrPayload {
                dialect_agnostic: true,
                models,
            },
        }
    }

    fn test_schema_model(table_name: &str, fk_targets: &[&str]) -> SchemaModel {
        SchemaModel {
            model_name: table_name.to_string(),
            table_name: table_name.to_string(),
            columns: vec![],
            foreign_keys: fk_targets
                .iter()
                .map(|to| SchemaForeignKey {
                    column: format!("{to}_id"),
                    to_table: (*to).to_string(),
                    to_column: "id".to_string(),
                    on_delete: None,
                    name: None,
                })
                .collect(),
            indexes: vec![],
            uniques: vec![],
            checks: vec![],
            table_checks: vec![],
            row_security: None,
        }
    }

    #[test]
    fn order_models_for_migration_sorts_fk_chain() {
        let modelset = test_modelset(vec![
            test_schema_model("child", &["parent"]),
            test_schema_model("parent", &["grandparent"]),
            test_schema_model("grandparent", &[]),
        ]);
        let mut registry = HashMap::new();
        registry.insert("Child".to_string(), test_registered("child"));
        registry.insert("Parent".to_string(), test_registered("parent"));
        registry.insert("Grandparent".to_string(), test_registered("grandparent"));

        let ordered = order_models_for_migration(registry, &modelset);
        let tables: Vec<&str> = ordered
            .iter()
            .map(|(_, model)| model.table_name.as_str())
            .collect();
        assert_eq!(tables, vec!["grandparent", "parent", "child"]);
    }

    #[test]
    fn order_models_for_migration_ignores_external_fk_targets() {
        let modelset = test_modelset(vec![test_schema_model("post", &["external_user"])]);
        let mut registry = HashMap::new();
        registry.insert("Post".to_string(), test_registered("post"));

        let ordered = order_models_for_migration(registry, &modelset);
        assert_eq!(ordered.len(), 1);
        assert_eq!(ordered[0].1.table_name, "post");
    }

    #[test]
    fn order_models_for_migration_appends_remaining_on_cycle() {
        let modelset = test_modelset(vec![
            test_schema_model("alpha", &["beta"]),
            test_schema_model("beta", &["alpha"]),
        ]);
        let mut registry = HashMap::new();
        registry.insert("Alpha".to_string(), test_registered("alpha"));
        registry.insert("Beta".to_string(), test_registered("beta"));

        let ordered = order_models_for_migration(registry, &modelset);
        assert_eq!(ordered.len(), 2);
        let tables: Vec<&str> = ordered
            .iter()
            .map(|(_, model)| model.table_name.as_str())
            .collect();
        assert!(tables.contains(&"alpha"));
        assert!(tables.contains(&"beta"));
    }

    #[test]
    fn order_models_for_migration_self_fk_is_not_an_ordering_constraint() {
        // #302: `znode` carries a self-referential FK and `areferrer`
        // (alphabetically first) requires it. The self-loop must not evict
        // the component from the dependency order.
        let modelset = test_modelset(vec![
            test_schema_model("znode", &["znode"]),
            test_schema_model("areferrer", &["znode"]),
        ]);
        let mut registry = HashMap::new();
        registry.insert("ZNode".to_string(), test_registered("znode"));
        registry.insert("AReferrer".to_string(), test_registered("areferrer"));

        let ordered = order_models_for_migration(registry, &modelset);
        let tables: Vec<&str> = ordered
            .iter()
            .map(|(_, model)| model.table_name.as_str())
            .collect();
        assert_eq!(tables, vec!["znode", "areferrer"]);
    }

    #[test]
    fn order_models_for_migration_treats_missing_modelset_entry_as_no_deps() {
        let modelset = test_modelset(vec![]);
        let mut registry = HashMap::new();
        registry.insert("Orphan".to_string(), test_registered("orphan"));

        let ordered = order_models_for_migration(registry, &modelset);
        assert_eq!(ordered.len(), 1);
        assert_eq!(ordered[0].1.table_name, "orphan");
    }

    #[test]
    fn test_composite_index_name_short() {
        assert_eq!(composite_index_name("users", &["a", "b"]), "idx_users_a_b");
    }

    #[test]
    fn test_composite_index_name_at_63_chars() {
        let pad: String = "x".repeat(55);
        let cols = [pad.as_str(), "y"];
        let result = composite_index_name("t", &cols);
        assert_eq!(result.chars().count(), 63);
        assert!(!result.ends_with("_idx") || result == format!("idx_t_{}_y", pad));
    }

    #[test]
    fn test_composite_index_name_truncation_above_63() {
        let long_a = "very_long_column_name_alpha_for_idx_truncation_test";
        let long_b = "very_long_column_name_beta_for_idx_truncation_test";
        let table = "verylongcompositeindexmodelnamefortruncation";
        let result = composite_index_name(table, &[long_a, long_b]);
        assert_eq!(result.chars().count(), 63);
        assert!(result.ends_with("_idx"));
    }

    #[test]
    fn test_composite_index_name_unicode_safe() {
        let table = "tbl_üñîçødé_with_long_table_name_for_truncation_check";
        let cols = ["α_column_one", "β_column_two_extended_for_overflow"];
        let result = composite_index_name(table, &cols);
        assert!(result.chars().count() <= 63);
    }

    #[test]
    fn test_db_check_constraint_name_short() {
        assert_eq!(db_check_constraint_name("doc", "format"), "ck_doc_format");
    }

    #[test]
    fn test_db_check_constraint_name_truncates_above_63() {
        let long_col = "a".repeat(70);
        let result = db_check_constraint_name("verylongtable", &long_col);
        assert_eq!(result.chars().count(), 63);
        assert!(result.ends_with("_ck"));
    }
}
