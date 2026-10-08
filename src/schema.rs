//! Schema registration and table-creation orchestration.
//!
//! Model registration flows through the SchemaIR column slice; CREATE TABLE is
//! emitted from the Python-compiled SchemaIR modelset via `ferro_migrate`.

use crate::backend::EngineHandle;
use crate::ddl_exec::{DdlError, DdlExecutor, Door, Failed, Unit};
use crate::migrate::{pass_attempt_warning, pass_lock_timeout_error};
use crate::state::{Dialect, MODEL_REGISTRY, engine_for_connection};
use ferro_migrate::plan::{
    hint_refusal_warning, pending_table_rename_warning, refuse_hints, table_rename_hint,
};
use ferro_schema_ir::{SchemaIrPayload, SchemaModel};
use pyo3::prelude::*;
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::sync::Arc;

/// Internal utility to create all registered tables in the database.
///
/// This is used by both the `connect(auto_migrate=True)` flow and the
/// manual `create_tables()` function.
///
/// Returns the tables that already existed when the pass started — the set the
/// reconciliation pass owns (ADR-0010) — and the tables it held back for a
/// rename ([`CreatePass`]). A table this pass created is already exactly the
/// model, so re-diffing it would only replay the create pass's own
/// backend-limitation warnings.
///
/// A declared table absent live whose `__ferro_renamed_from__` names a live
/// table is a rename, not a creation (ADR-0032): it is not created, and
/// neither is a new table that references it, which can only be created once
/// the rename has run. The reconciliation pass renames it under
/// `migrate_updates`; a pass that does not reconcile warns, naming both doors,
/// and leaves the database as it is — never an empty twin beside the old
/// table.
///
/// On Postgres every statement runs under `ddl` (ADR-0044): a new table's
/// `REFERENCES "parent"` takes a lock on a parent that already exists, and
/// waiting behind an open write on it would queue every query on the parent
/// behind the `CREATE TABLE`. A table that times out is rolled back and
/// created again from the top; an enum type statement re-runs alone.
///
/// # Errors
/// Returns a `PyErr` if the SQL execution fails; `OperationalError` naming
/// `ddl_lock_timeout` after the last attempt.
pub async fn internal_create_tables(
    engine: Arc<EngineHandle>,
    reconciliation_follows: bool,
    ddl: &DdlExecutor,
) -> PyResult<CreatePass> {
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
    let held_back = tables_awaiting_rename(&model_refs, &existing_tables);
    if !reconciliation_follows {
        warn_pending_renames(&modelset.payload, &existing_tables, &held_back);
    }
    let mut to_create = Vec::new();
    for model in ferro_migrate::order_models_for_create(&model_refs) {
        if held_back.contains_key(&model.table_name) {
            crate::log_debug(format!(
                "Ferro Engine: Table '{}' waits on a table rename — not created",
                model.table_name
            ));
            continue;
        }
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
        to_create.push((model, emission));
    }

    // Every native enum type the created tables declare comes into being
    // ahead of every table, by type name — the one planner's order (a type
    // comes into being before any table op that uses it), so a generated
    // migration's up file and this pass execute the same sequence (I-1). Each
    // statement is the table emission's own guarded `CREATE TYPE`, run once
    // per type, in autocommit like the reconciliation pass's type statements.
    let mut type_guards: std::collections::BTreeMap<String, (&str, &String)> =
        std::collections::BTreeMap::new();
    for (model, emission) in &to_create {
        for guard in &emission.pre_create_sqls {
            let type_name = model
                .columns
                .iter()
                .find_map(
                    |col| match ferro_ddl_lowering::resolve_column_storage(col, dialect) {
                        Ok(ferro_ddl_lowering::ResolvedStorage::PgEnum { type_name, labels })
                            if *guard
                                == ferro_ddl_lowering::render_pg_enum_create_type(
                                    &type_name, &labels,
                                ) =>
                        {
                            Some(type_name)
                        }
                        _ => None,
                    },
                )
                .ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "CREATE TABLE emission for '{}' carries a pre-create statement that \
                         is no enum type of its columns: {guard}",
                        model.table_name
                    ))
                })?;
            type_guards
                .entry(type_name)
                .or_insert((model.table_name.as_str(), guard));
        }
    }
    for (table, guard) in type_guards.values() {
        let (table, guard) = (*table, *guard);
        ddl.run(
            &engine,
            Unit::Unwrapped,
            Door::Pass(table),
            &[guard],
            |attempt| crate::emit_user_warning_always(&pass_attempt_warning(table, &attempt)),
            None,
        )
        .await
        .map_err(|err| match err {
            DdlError::LockTimeout(timeout) => pass_lock_timeout_error(table, &timeout),
            DdlError::Failed(failure) => {
                create_step_error(table, "enum type", guard, failure.error)
            }
        })?;
    }

    for (model, emission) in &to_create {
        create_one_table(&engine, model, emission, ddl).await?;

        for warning in &emission.warnings {
            crate::emit_user_warning(warning);
        }

        crate::log_debug(format!("✅ Ferro Engine: Table '{}' created", model.table_name));
    }

    Ok(CreatePass {
        existing: existing_tables,
        held_back: held_back.into_keys().collect(),
    })
}

/// What the create pass leaves to the reconciliation pass.
pub struct CreatePass {
    /// Every table that existed live when the pass started (ADR-0010).
    pub existing: HashSet<String>,
    /// The declared tables the pass did not create because they wait on a
    /// table rename: a table whose live hint names a live table, and every
    /// new table that references one, directly or through another.
    pub held_back: BTreeSet<String>,
}

/// The declared tables absent live that wait on a table rename, each with
/// the hinted tables it waits on: a table whose `__ferro_renamed_from__` hint
/// is live against `live` ([`table_rename_hint`], the planner's own liveness
/// rule) waits on itself, and a table absent live that references a waiting
/// table waits on what that one waits on.
fn tables_awaiting_rename(
    models: &[&SchemaModel],
    live: &HashSet<String>,
) -> BTreeMap<String, BTreeSet<String>> {
    let mut waiting: BTreeMap<String, BTreeSet<String>> = models
        .iter()
        .filter(|model| table_rename_hint(model, |table| live.contains(table)).is_some())
        .map(|model| {
            (
                model.table_name.clone(),
                BTreeSet::from([model.table_name.clone()]),
            )
        })
        .collect();
    // A fixed point, so a reference cycle among new tables settles too.
    loop {
        let mut changed = false;
        for model in models {
            if live.contains(&model.table_name) {
                continue;
            }
            let inherited: BTreeSet<String> = model
                .foreign_keys
                .iter()
                .filter_map(|fk| waiting.get(&fk.to_table))
                .flatten()
                .cloned()
                .collect();
            if inherited.is_empty() {
                continue;
            }
            let entry = waiting.entry(model.table_name.clone()).or_default();
            let before = entry.len();
            entry.extend(inherited);
            changed |= entry.len() != before;
        }
        if !changed {
            return waiting;
        }
    }
}

/// The create pass's word on the tables it held back when no reconciliation
/// follows: a refused hint's refusal, once, as the planner words it; else,
/// per live table hint, which tables wait on it and the two doors that run
/// the rename.
fn warn_pending_renames(
    declared: &SchemaIrPayload,
    live: &HashSet<String>,
    held_back: &BTreeMap<String, BTreeSet<String>>,
) {
    if held_back.is_empty() {
        return;
    }
    if let Err(refusal) = refuse_hints(declared) {
        crate::emit_user_warning_always(&hint_refusal_warning(&refusal));
        return;
    }
    for model in &declared.models {
        let Some(old) = table_rename_hint(model, |table| live.contains(table)) else {
            continue;
        };
        let new = &model.table_name;
        let dependents: Vec<String> = held_back
            .iter()
            .filter(|(table, roots)| *table != new && roots.contains(new))
            .map(|(table, _)| table.clone())
            .collect();
        crate::emit_user_warning_always(&pending_table_rename_warning(old, new, &dependents));
    }
}

/// Execute one model's create emission — `CREATE TABLE` and every
/// post-create artifact (indexes, checks, row-security flags and
/// policies) — as ONE unit. Its enum types were created ahead of every table
/// by [`internal_create_tables`].
///
/// On Postgres this is a single transaction, mirroring the reconciliation
/// pass's per-table transaction (FF-G G3), under the DDL lock timeout
/// (`SET LOCAL lock_timeout` first; a timeout rolls the table back and
/// creates it again): a table ends fully created or not created at all. That is not a nicety for row security, it is the whole
/// contract. `ENABLE`/`FORCE ROW LEVEL SECURITY` land before the policies, so a
/// `CREATE POLICY` that fails halfway would otherwise leave a live table with
/// row security forced on and **zero** policies — default-deny for every role
/// including the owner — that the create pass can never repair, because it only
/// ever touches missing tables (ADR-0010). Rolling the table back instead
/// leaves the database exactly as it was and the connect fails loudly, naming
/// the statement.
///
/// SQLite runs the same statements unwrapped, one at a time, matching the
/// reconciliation pass; it emits no row-security DDL at all, so the lockout
/// shape cannot occur there.
async fn create_one_table(
    engine: &Arc<EngineHandle>,
    model: &ferro_schema_ir::SchemaModel,
    emission: &ferro_migrate::CreateTableEmission,
    ddl: &DdlExecutor,
) -> PyResult<()> {
    let table = model.table_name.as_str();
    let unit = match engine.backend() {
        Dialect::Postgres => Unit::Transactional,
        Dialect::Sqlite => Unit::Unwrapped,
    };
    let statements: Vec<&String> = std::iter::once(&emission.create_sql)
        .chain(&emission.post_create_sqls)
        .collect();
    let result = ddl
        .run(
            engine,
            unit,
            Door::Pass(table),
            &statements,
            |attempt| crate::emit_user_warning_always(&pass_attempt_warning(table, &attempt)),
            None,
        )
        .await;
    result.map(|_| ()).map_err(|err| match err {
        DdlError::LockTimeout(timeout) => pass_lock_timeout_error(table, &timeout),
        DdlError::Failed(Failed {
            index: Some(index),
            error,
        }) => {
            let step = if index == 0 { "table" } else { "artifact" };
            create_step_error(table, step, statements[index], error)
        }
        DdlError::Failed(Failed { index: None, error }) => crate::errors::map_db_error(
            &format!("Auto-migrate failed to create table '{table}'"),
            error,
        ),
    })
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
/// Returns an awaitable object (Python coroutine). Like `connect()`'s
/// auto-migrate flags it runs under the run lock and refuses a database
/// governed by ferro migrations (ADR-0038); `tracking_schemas` are the
/// project's configured `tracking_schema`s, and `ddl_lock_timeout_s` its
/// `ddl_lock_timeout` in seconds, which every `CREATE` waits for locks under
/// on Postgres (ADR-0044; `0` disables).
///
/// # Errors
/// Returns a `PyErr` if the engine is not initialized, the database is
/// governed by ferro migrations, or SQL execution fails.
#[pyfunction]
#[pyo3(signature = (using=None, tracking_schemas=Vec::new(), ddl_lock_timeout_s=5.0))]
pub fn create_tables(
    py: Python<'_>,
    using: Option<String>,
    tracking_schemas: Vec<String>,
    ddl_lock_timeout_s: f64,
) -> PyResult<Bound<'_, PyAny>> {
    let opts = crate::migrate::MigrateOptions::laddered(false, false)
        .with_ddl_lock_timeout_seconds(ddl_lock_timeout_s)?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = engine_for_connection(using)?;
        // `create_tables()` is the create pass on its own (no `updates`):
        // nothing reconciles an existing table afterwards, so a declaration
        // on one is reported. It shares the lock-then-guard path.
        crate::migrate::internal_migrate(
            engine,
            opts,
            &tracking_schemas,
            crate::migrate::AutoMigrateDoor::CreateTables,
        )
        .await
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
            renamed_from: None,
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
            enum_renamed_labels: Default::default(),
            default_factory: None,
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
    use ferro_ddl_lowering::{composite_index_name, db_check_constraint_name};

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

    /// The SQLite twin of `tests/test_ddl_unprepared.py`: the create pass's
    /// `CREATE TABLE` and `CREATE INDEX` describe a schema the next migration
    /// may change, so neither may stay prepared on the connection it ran on
    /// (`docs/solutions/patterns/ddl-on-live-engine.md`).
    mod unprepared {
        use super::super::create_one_table;
        use crate::backend::{EngineConnection, EngineHandle, PoolSpec};
        use crate::ddl_exec::DdlExecutor;
        use crate::session_settings::SettingsDelivery;
        use ferro_ddl_lowering::Dialect;
        use ferro_schema_ir::{SchemaColumn, SchemaIndex, SchemaModel};
        use sqlx::Connection;
        use std::sync::Arc;
        use std::sync::atomic::AtomicUsize;

        fn column(name: &str, logical_type: &str, primary_key: bool, index: bool) -> SchemaColumn {
            SchemaColumn {
                renamed_from: None,
                name: name.to_string(),
                logical_type: logical_type.to_string(),
                db_type: None,
                db_type_explicit: None,
                nullable: false,
                primary_key,
                autoincrement: primary_key,
                unique: false,
                index,
                default: None,
                format: None,
                enum_values: None,
                enum_type_name: None,
                postgres_native_enum: false,
                enum_renamed_labels: Default::default(),
                default_factory: None,
            }
        }

        #[tokio::test]
        async fn the_create_pass_leaves_no_statement_in_the_connection_cache() {
            let engine = Arc::new(
                EngineHandle::connect(PoolSpec {
                    backend: Dialect::Sqlite,
                    url: "sqlite::memory:".to_string(),
                    search_path: None,
                    max_connections: 1,
                    min_connections: 0,
                    settings_delivery: SettingsDelivery::Transaction,
                    pins: Arc::new(AtomicUsize::new(0)),
                })
                .await
                .expect("in-memory engine"),
            );
            let model = SchemaModel {
                renamed_from: None,
                model_name: "Widget".to_string(),
                table_name: "widget".to_string(),
                columns: vec![
                    column("id", "integer", true, false),
                    column("name", "string", false, true),
                ],
                foreign_keys: vec![],
                indexes: vec![SchemaIndex {
                    name: "idx_widget_name".to_string(),
                    columns: vec!["name".to_string()],
                    unique: false,
                }],
                uniques: vec![],
                checks: vec![],
                table_checks: vec![],
                row_security: None,
            };
            let emission =
                ferro_migrate::render_create_table(&model, Dialect::Sqlite).expect("emission");
            assert!(
                !emission.post_create_sqls.is_empty(),
                "the index is created too"
            );

            create_one_table(&engine, &model, &emission, &DdlExecutor::new(None))
                .await
                .expect("created");

            let Ok(EngineConnection::Sqlite(conn)) =
                crate::ddl_exec::pool_connection(&engine).await
            else {
                panic!("the engine's one SQLite connection");
            };
            assert_eq!(conn.cached_statements_size(), 0);
        }
    }

    mod awaiting_rename {
        use super::super::tables_awaiting_rename;
        use ferro_schema_ir::{SchemaForeignKey, SchemaModel};
        use std::collections::{BTreeMap, BTreeSet, HashSet};

        /// A bare model `table` that references each of `refs`, hinted as
        /// renamed from `from` when given.
        fn model(table: &str, refs: &[&str], from: Option<&str>) -> SchemaModel {
            SchemaModel {
                renamed_from: from.map(str::to_string),
                model_name: table.to_string(),
                table_name: table.to_string(),
                columns: vec![],
                foreign_keys: refs
                    .iter()
                    .map(|to| SchemaForeignKey {
                        column: format!("{to}_id"),
                        to_table: (*to).to_string(),
                        to_column: "id".to_string(),
                        on_delete: None,
                        name: None,
                        renamed_from: None,
                    })
                    .collect(),
                indexes: vec![],
                uniques: vec![],
                checks: vec![],
                table_checks: vec![],
                row_security: None,
            }
        }

        fn live(tables: &[&str]) -> HashSet<String> {
            tables.iter().map(|t| (*t).to_string()).collect()
        }

        fn waiting(
            models: &[SchemaModel],
            live: &HashSet<String>,
        ) -> BTreeMap<String, BTreeSet<String>> {
            let refs: Vec<&SchemaModel> = models.iter().collect();
            tables_awaiting_rename(&refs, live)
        }

        fn roots(entries: &[(&str, &[&str])]) -> BTreeMap<String, BTreeSet<String>> {
            entries
                .iter()
                .map(|(table, on)| {
                    (
                        (*table).to_string(),
                        on.iter().map(|t| (*t).to_string()).collect(),
                    )
                })
                .collect()
        }

        #[test]
        fn a_grandchild_waits_through_its_parent() {
            // a (renamed from old_a) <- b <- c, declared child-first so one
            // pass over the models cannot settle it.
            let models = [
                model("c", &["b"], None),
                model("b", &["a"], None),
                model("a", &[], Some("old_a")),
            ];
            assert_eq!(
                waiting(&models, &live(&["old_a"])),
                roots(&[("a", &["a"]), ("b", &["a"]), ("c", &["a"])])
            );
        }

        #[test]
        fn each_dependent_waits_on_the_root_it_descends_from() {
            let models = [
                model("both", &["ba", "cx"], None),
                model("cx", &["x"], None),
                model("ba", &["a"], None),
                model("a", &[], Some("old_a")),
                model("x", &[], Some("old_x")),
            ];
            assert_eq!(
                waiting(&models, &live(&["old_a", "old_x"])),
                roots(&[
                    ("a", &["a"]),
                    ("x", &["x"]),
                    ("ba", &["a"]),
                    ("cx", &["x"]),
                    ("both", &["a", "x"]),
                ])
            );
        }

        #[test]
        fn a_reference_cycle_between_held_tables_settles() {
            // b <-> c, and b also references the renamed a.
            let models = [
                model("c", &["b"], None),
                model("b", &["a", "c"], None),
                model("a", &[], Some("old_a")),
            ];
            assert_eq!(
                waiting(&models, &live(&["old_a"])),
                roots(&[("a", &["a"]), ("b", &["a"]), ("c", &["a"])])
            );
        }

        #[test]
        fn a_table_referencing_only_a_non_held_table_is_not_held() {
            let models = [
                model("a", &[], Some("old_a")),
                model("other", &[], None),
                model("child", &["other"], None),
            ];
            assert_eq!(waiting(&models, &live(&["old_a"])), roots(&[("a", &["a"])]));
        }

        #[test]
        fn a_live_table_never_waits_and_an_inert_hint_holds_nothing() {
            // `b` is live already, so it is not held although it references
            // `a`; `z`'s hint is inert (its old table is not live).
            let models = [
                model("a", &[], Some("old_a")),
                model("b", &["a"], None),
                model("z", &[], Some("old_z")),
                model("zc", &["z"], None),
            ];
            assert_eq!(
                waiting(&models, &live(&["old_a", "b"])),
                roots(&[("a", &["a"])])
            );
        }
    }
}
