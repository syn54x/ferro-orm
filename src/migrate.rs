//! Auto-migrate schema diffing and execution.
//!
//! Extends `connect(auto_migrate=True)` beyond table creation: with
//! `migrate_updates`, missing model columns are added to existing tables
//! (plus, on Postgres, type/nullability reconciliation); with
//! `migrate_destructive`, live columns that no longer exist on the model are
//! dropped. Capability matrix and semantics are documented on the Python
//! `ferro.connect` / `ferro.migrate` APIs.
//!
//! Column DDL for auto-migrate is planned via SchemaIR diffing (`plan_from_ir`) and
//! lowered by `ferro-migrate` (`emit_sql_with_ir`), so an auto-migrated database
//! matches a freshly created one (AGENTS.md § I-1).

use crate::backend::EngineHandle;
use ferro_ddl_lowering::{
    Dialect, ResolvedStorage, extra_enum_labels, extra_enum_labels_warning,
    information_schema_to_db_type_token, missing_enum_labels, render_pg_enum_add_value,
    resolve_column_storage,
};
use ferro_migrate::{MigrationOp, emit_sql_with_ir, plan_check_rebuilds, plan_from_ir, plan_missing_checks};
use ferro_schema_ir::{
    IrEnvelope, SchemaCheck, SchemaColumn, SchemaForeignKey, SchemaIndex, SchemaIrPayload,
    SchemaModel, SchemaUnique,
};
use crate::introspect::{
    LiveCheck, LiveColumn, LiveForeignKey, LiveIndex, live_enum_type_labels, live_table_checks,
    live_table_columns, live_table_foreign_keys, live_table_indexes, quote_ident,
    sqlite_indexes_covering_column,
};
use crate::schema::{internal_create_tables, order_models_for_migration};
use crate::state::{MODEL_REGISTRY, engine_for_connection};
use pyo3::prelude::*;
use std::sync::Arc;

fn schema_ir_column<'a>(
    envelope: &'a IrEnvelope<SchemaIrPayload>,
    table: &str,
    column: &str,
) -> Option<&'a SchemaColumn> {
    envelope
        .payload
        .models
        .iter()
        .find(|model| model.table_name == table)
        .and_then(|model| model.columns.iter().find(|col| col.name == column))
}

/// Atomically install the column registry, schema modelset, and modelset
/// fingerprint from one assembled payload (#244).
///
/// This is the single Rust registration sync seam: `connect()`,
/// `create_tables()`, and `migrate()` route through it. The heavy lifting
/// (build-then-swap, the fingerprint gate, the push counter) lives in
/// [`crate::state::install_registration`]; this wrapper only parses and
/// validates the payload envelope.
///
/// Returns `true` when an install was performed, `false` when the fingerprint
/// gate skipped it.
///
/// # Errors
/// `PyValueError` when the JSON is invalid, the envelope is not a `schema` IR,
/// or a model's columns cannot compile; `PyRuntimeError` on a poisoned lock.
#[pyfunction]
#[pyo3(name = "_install_registration")]
pub fn _install_registration(payload_json: String, fingerprint: String) -> PyResult<bool> {
    // Fast path: a warm reconnect skips the swap, so it need not pay the
    // payload parse. `install_registration` re-checks the gate authoritatively.
    if crate::state::installed_fingerprint_matches(&fingerprint)? {
        return Ok(false);
    }
    let envelope: IrEnvelope<SchemaIrPayload> = serde_json::from_str(&payload_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("invalid registration payload json: {e}"))
    })?;
    if envelope.ir_kind != "schema" {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "expected ir_kind 'schema', got '{}'",
            envelope.ir_kind
        )));
    }
    crate::state::install_registration(envelope, fingerprint)
}

/// Test-only instrument: the process-wide bulk-install count (#244).
///
/// Mirrors `_catalog_query_count_for_test` — a single counter bumped at one
/// choke point (`install_registration`, only on an actual swap, never on a
/// fingerprint-skip) and read from Python to assert install cardinality.
#[pyfunction]
#[pyo3(name = "_bulk_install_count_for_test")]
pub fn _bulk_install_count_for_test() -> u64 {
    crate::state::BULK_INSTALL_COUNT.load(std::sync::atomic::Ordering::Relaxed)
}

/// Test-only instrument: count of models in the Rust column registry (#246).
///
/// Mirrors `_bulk_install_count_for_test` — a single read at the registry
/// store so tests can assert provisional import leaves Rust empty until the
/// first bulk install.
#[pyfunction]
#[pyo3(name = "_rust_model_registry_count_for_test")]
pub fn _rust_model_registry_count_for_test() -> PyResult<usize> {
    let registry = MODEL_REGISTRY.read().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
    })?;
    Ok(registry.len())
}

/// Test-only helper: clear the pushed SchemaIR modelset (and its recorded
/// fingerprint, so the gate can never match state the runtime no longer holds)
/// so the fail-loud path in `internal_create_tables` / `internal_migrate` can be
/// exercised from Python (a missing modelset must raise, never silently create
/// nothing).
#[pyfunction]
#[pyo3(name = "_clear_schema_ir_modelset_for_test")]
pub fn _clear_schema_ir_modelset_for_test() -> PyResult<()> {
    let mut modelset_guard = crate::state::SCHEMA_IR_MODELSET.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock SchemaIR modelset")
    })?;
    let mut fingerprint_guard = crate::state::INSTALLED_FINGERPRINT.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock installed fingerprint")
    })?;
    *modelset_guard = None;
    *fingerprint_guard = None;
    Ok(())
}

/// Narrow the declared modelset to a single-model envelope for `table`,
/// matching the shape `plan_table_migration` expects (old vs new are both
/// single-model). Returns None if the model is absent from the modelset.
fn declared_envelope_for(
    modelset: &IrEnvelope<SchemaIrPayload>,
    table: &str,
) -> Option<IrEnvelope<SchemaIrPayload>> {
    let model = modelset.payload.models.iter().find(|m| m.table_name == table)?;
    Some(IrEnvelope {
        ir_kind: "schema".to_string(),
        ir_version: modelset.ir_version,
        payload: SchemaIrPayload {
            dialect_agnostic: modelset.payload.dialect_agnostic,
            models: vec![model.clone()],
        },
    })
}

fn live_columns_to_schema_ir(
    table_lower: &str,
    live: &[LiveColumn],
    live_indexes: &[LiveIndex],
    live_foreign_keys: &[LiveForeignKey],
    backend: Dialect,
) -> IrEnvelope<SchemaIrPayload> {
    let dialect = backend;
    let mut columns: Vec<SchemaColumn> = live
        .iter()
        .map(|col| SchemaColumn {
            name: col.name.clone(),
            logical_type: "unknown".to_string(),
            db_type: Some(information_schema_to_db_type_token(
                &col.declared_type,
                col.char_max_len,
                dialect,
            )),
            db_type_explicit: None,
            nullable: col.is_nullable,
            primary_key: col.is_primary_key,
            autoincrement: false,
            unique: false,
            index: false,
            default: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: col.is_enum_udt,
        })
        .collect();
    columns.sort_by(|a, b| a.name.cmp(&b.name));
    IrEnvelope {
        ir_kind: "schema".to_string(),
        ir_version: 1,
        payload: SchemaIrPayload {
            dialect_agnostic: true,
            models: vec![SchemaModel {
                model_name: table_lower.to_string(),
                table_name: table_lower.to_string(),
                columns,
                foreign_keys: {
                    let mut fks: Vec<SchemaForeignKey> = live_foreign_keys
                        .iter()
                        .map(|fk| SchemaForeignKey {
                            column: fk.column.clone(),
                            to_table: fk.to_table.clone(),
                            to_column: fk.to_column.clone(),
                            on_delete: Some(fk.on_delete.clone()),
                            name: fk.name.clone(),
                        })
                        .collect();
                    fks.sort_by(|a, b| a.column.cmp(&b.column));
                    fks
                },
                indexes: live_indexes.iter().map(|i| SchemaIndex {
                    name: i.name.clone(), columns: i.columns.clone(), unique: i.unique,
                }).collect(),
                uniques: Vec::<SchemaUnique>::new(),
                checks: Vec::<SchemaCheck>::new(),
                table_checks: Vec::new(),
            }],
        },
    }
}

/// Which migration behaviors beyond table creation are enabled.
#[derive(Clone, Copy, Debug, Default)]
pub struct MigrateOptions {
    /// Add missing model columns to existing tables; on Postgres, also
    /// reconcile column type and nullability drift.
    pub updates: bool,
    /// Drop live columns that no longer exist on the model. Implies `updates`.
    pub destructive: bool,
}

impl MigrateOptions {
    /// Apply the flag ladder: `destructive` ⇒ `updates`.
    pub fn laddered(updates: bool, destructive: bool) -> Self {
        Self {
            updates: updates || destructive,
            destructive,
        }
    }
}

/// The DDL and diagnostics produced by diffing one table.
#[derive(Debug)]
pub struct MigrationPlan {
    /// Ready-to-execute DDL statements, in order.
    pub statements: Vec<String>,
    /// Columns to drop (destructive mode). Kept separate from `statements`
    /// because the executor must resolve live index dependencies first.
    pub drop_columns: Vec<String>,
    /// Human-readable notes emitted as Python `UserWarning`s.
    pub warnings: Vec<String>,
}

impl MigrationPlan {
    fn new() -> Self {
        Self {
            statements: Vec::new(),
            drop_columns: Vec::new(),
            warnings: Vec::new(),
        }
    }

    fn is_empty(&self) -> bool {
        self.statements.is_empty() && self.drop_columns.is_empty()
    }
}

/// Diff one registered model schema against its live table and produce the
/// DDL plan. Pure with respect to the database — callers introspect first.
///
/// # Errors
/// Returns a `PyErr` for changes that cannot be applied safely: adding a
/// primary-key column, or adding a NOT NULL column without a usable literal
/// default. These abort the migration ("fail loudly").
pub fn plan_table_migration(
    table_lower: &str,
    declared: &IrEnvelope<SchemaIrPayload>,
    live: &[LiveColumn],
    live_indexes: &[LiveIndex],
    live_foreign_keys: &[LiveForeignKey],
    live_checks: &[LiveCheck],
    backend: Dialect,
    opts: MigrateOptions,
) -> PyResult<MigrationPlan> {
    if !opts.updates {
        return Ok(MigrationPlan::new());
    }

    let old_ir =
        live_columns_to_schema_ir(table_lower, live, live_indexes, live_foreign_keys, backend);
    let new_ir = declared;
    let mut typed_plan = plan_from_ir(&old_ir, new_ir, backend);
    // Check addition (#343; ADR-0013) is planned after the column diff so a
    // CHECK over a newly added column lands after its ADD COLUMN. Live CHECKs
    // travel beside the IR rather than inside it: their bodies are the
    // backend's own rendering, and the IR carries exactly one body language
    // (AGENTS.md § I-1).
    let live_check_names: Vec<String> =
        live_checks.iter().map(|check| check.name.clone()).collect();
    typed_plan.operations.extend(plan_missing_checks(
        table_lower,
        &old_ir,
        new_ir,
        &live_check_names,
    ));
    // Body drift (#344; ADR-0015) is planned after missing-name adds. Live
    // catalog text stays beside the IR; only ferro-owned names are eligible
    // for rebuild (a user-owned CHECK is never dropped).
    let live_for_rebuild: Vec<(String, String)> = live_checks
        .iter()
        .filter(|check| check.ferro_owned)
        .map(|check| (check.name.clone(), check.definition.clone()))
        .collect();
    typed_plan.operations.extend(plan_check_rebuilds(
        table_lower,
        new_ir,
        &live_for_rebuild,
    ));

    if !opts.destructive {
        typed_plan
            .operations
            .retain(|op| !matches!(op, MigrationOp::DropColumn { .. } | MigrationOp::DropIndex { .. }));
    }

    let mut plan = MigrationPlan::new();
    let mut exec_ops = Vec::new();

    for operation in typed_plan.operations {
        if let MigrationOp::DropColumn { table, column } = &operation {
            let Some(old_col) = schema_ir_column(&old_ir, table, column) else {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Cannot drop column '{}.{}': column metadata missing from live IR context.",
                    table, column
                )));
            };
            if old_col.primary_key {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Cannot drop column '{}.{}': it is part of the primary key. \
                     Primary-key changes must be migrated with Alembic.",
                    table, column
                )));
            }
            plan.drop_columns.push(column.clone());
        } else {
            exec_ops.push(operation);
        }
    }

    let exec_plan = ferro_migrate::MigrationPlan {
        operations: exec_ops,
        warnings: typed_plan.warnings,
    };
    let emission = emit_sql_with_ir(&exec_plan, &old_ir, new_ir, backend)
        .map_err(|err| pyo3::exceptions::PyValueError::new_err(err.message))?;

    plan.statements = emission.statements;
    plan.warnings = emission.warnings;
    Ok(plan)
}

/// Render the `ALTER TABLE ... DROP COLUMN ...` DDL for one column drop.
/// Shared by the SQLite path in [`execute_drop_column`] and the Postgres
/// per-table transaction in [`internal_migrate`].
fn render_drop_column_sql(table_lower: &str, col_name: &str) -> String {
    format!(
        "ALTER TABLE {} DROP COLUMN {}",
        quote_ident(table_lower),
        quote_ident(col_name)
    )
}

/// Map a column-drop execution failure to a `PyErr` with a consistent,
/// actionable message. Shared by the SQLite path in [`execute_drop_column`]
/// and the Postgres per-table transaction in [`internal_migrate`].
fn map_drop_column_error(table_lower: &str, col_name: &str, e: sqlx::Error) -> PyErr {
    crate::errors::map_db_error(
        &format!(
            "Cannot drop column '{}.{}' (columns referenced by constraints, foreign \
             keys, triggers, or views must be migrated with Alembic)",
            table_lower, col_name
        ),
        e,
    )
}

/// Drop one column, resolving SQLite index dependencies first.
///
/// Explicit indexes covering the column are orphaned by its removal and are
/// dropped beforehand (SQLite refuses `DROP COLUMN` on an indexed column).
/// Constraint autoindexes cannot be dropped separately, so their presence is
/// a hard error, as is any remaining engine refusal (CHECK references,
/// triggers, views, inbound foreign keys).
async fn execute_drop_column(
    engine: &EngineHandle,
    table_lower: &str,
    col_name: &str,
    backend: Dialect,
) -> PyResult<()> {
    if backend == Dialect::Sqlite {
        let indexes = sqlite_indexes_covering_column(engine, table_lower, col_name).await?;
        if let Some(blocking) = indexes.iter().find(|index| index.origin != "c") {
            let constraint = match blocking.origin.as_str() {
                "u" => "a UNIQUE constraint",
                "pk" => "the PRIMARY KEY",
                _ => "a table constraint",
            };
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Cannot drop column '{}.{}': it is enforced by {} ('{}'), which SQLite \
                 cannot drop separately from the table definition. Use Alembic for this \
                 migration.",
                table_lower, col_name, constraint, blocking.name
            )));
        }
        for index in &indexes {
            let sql = format!("DROP INDEX IF EXISTS {}", quote_ident(&index.name));
            engine.execute_sql_unprepared(&sql).await.map_err(|e| {
                crate::errors::map_db_error(
                    &format!(
                        "Auto-migrate failed dropping index '{}' (required to drop column \
                         '{}.{}')",
                        index.name, table_lower, col_name
                    ),
                    e,
                )
            })?;
        }
    }

    let sql = render_drop_column_sql(table_lower, col_name);
    engine
        .execute_sql_unprepared(&sql)
        .await
        .map_err(|e| map_drop_column_error(table_lower, col_name, e))?;
    Ok(())
}

/// Run the full auto-migrate pass: create missing tables, then (per
/// `MigrateOptions`) reconcile existing tables with the registered models.
///
/// After any ALTER/DROP executed, the engine pool is refreshed so no
/// connection can serve a statement prepared against the pre-DDL schema.
///
/// # Errors
/// Returns a `PyErr` if introspection, DDL execution, or the pool refresh
/// fails, or if the diff contains a change that cannot be applied safely.
pub async fn internal_migrate(engine: Arc<EngineHandle>, opts: MigrateOptions) -> PyResult<()> {
    let tables_before_create = internal_create_tables(engine.clone()).await?;
    if !opts.updates {
        return Ok(());
    }

    let schemas = {
        let registry = MODEL_REGISTRY.read().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
        })?;
        registry.clone()
    };
    let modelset = {
        let guard = crate::state::SCHEMA_IR_MODELSET.read().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock SchemaIR modelset")
        })?;
        guard.clone().ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err(
            "SchemaIR modelset not set — connect()/migrate() must push it before migrating"
        ))?
    };
    let backend = engine.backend();

    let mut warnings = Vec::new();
    let mut ddl_ran = false;

    // Label addition (ADR-0011): reconcile ferro-owned enum types before any
    // table's plan. Per-type, not per-table (a shared StrEnum reconciles
    // once), and outside the per-table transactions below — `ALTER TYPE ...
    // ADD VALUE` is non-transactional before PG12 and its label is unusable
    // until commit on PG12+; autocommit execution here means every label is
    // committed before a table plan (e.g. a new column defaulting to it)
    // can reference it.
    if backend == Dialect::Postgres {
        ddl_ran |= add_missing_enum_labels(&engine, &modelset, &mut warnings).await?;
    }

    for (_name, model) in order_models_for_migration(schemas, &modelset) {
        let table_lower = model.table_name.clone();
        // ADR-0010: the reconciliation pass owns tables that already existed.
        // A table the create pass built in this same run is already exactly the
        // model — re-diffing it can only replay that pass's own
        // backend-limitation warnings (e.g. the SQLite `db_check` elision).
        if !tables_before_create.contains(&table_lower) {
            continue;
        }
        let Some(live) = live_table_columns(&engine, &table_lower).await? else {
            // A table that vanished between the create pass and here.
            continue;
        };
        let live_indexes = live_table_indexes(&engine, &table_lower).await?;
        let live_foreign_keys = live_table_foreign_keys(&engine, &table_lower).await?;
        let live_checks = live_table_checks(&engine, &table_lower).await?;

        let Some(declared) = declared_envelope_for(&modelset, &table_lower) else { continue };
        let mut plan = plan_table_migration(
            &table_lower,
            &declared,
            &live,
            &live_indexes,
            &live_foreign_keys,
            &live_checks,
            backend,
            opts,
        )?;
        if plan.is_empty() {
            warnings.append(&mut plan.warnings);
            continue;
        }

        if backend == Dialect::Postgres {
            // FF-G G3: Postgres DDL is transactional — run this table's whole
            // plan in one transaction so a mid-plan failure leaves the table
            // exactly as it was. Per-table, not whole-run: every table ends
            // fully migrated or untouched, so a failed run is safely
            // re-runnable. (SQLite keeps statement-at-a-time execution below;
            // its scope is documented on connect()/migrate().)
            let mut conn = engine.begin_transaction_connection().await.map_err(|e| {
                crate::errors::map_db_error(
                    &format!(
                        "Auto-migrate failed to open a transaction for table '{}'",
                        table_lower
                    ),
                    e,
                )
            })?;
            let table_result: PyResult<()> = async {
                for sql in &plan.statements {
                    conn.execute_sql_unprepared(sql).await.map_err(|e| {
                        crate::errors::map_db_error(
                            &format!(
                                "Auto-migrate DDL failed for table '{}' (statement: {})",
                                table_lower, sql
                            ),
                            e,
                        )
                    })?;
                }
                for col_name in &plan.drop_columns {
                    // Postgres needs no index pre-scan (that path is
                    // SQLite-only in execute_drop_column).
                    conn.execute_sql_unprepared(&render_drop_column_sql(&table_lower, col_name))
                        .await
                        .map_err(|e| map_drop_column_error(&table_lower, col_name, e))?;
                }
                Ok(())
            }
            .await;
            match table_result {
                Ok(()) => {
                    conn.commit().await.map_err(|e| {
                        crate::errors::map_db_error(
                            &format!(
                                "Auto-migrate failed to commit DDL for table '{}'",
                                table_lower
                            ),
                            e,
                        )
                    })?;
                    ddl_ran = true;
                }
                Err(err) => {
                    if let Err(rollback_err) = conn.rollback().await {
                        crate::log_debug(format!(
                            "⚠️ Ferro Engine: rollback after failed migration of '{}' also \
                             failed: {}",
                            table_lower, rollback_err
                        ));
                    }
                    return Err(err);
                }
            }
        } else {
            for sql in &plan.statements {
                engine.execute_sql_unprepared(sql).await.map_err(|e| {
                    crate::errors::map_db_error(
                        &format!(
                            "Auto-migrate DDL failed for table '{}' (statement: {})",
                            table_lower, sql
                        ),
                        e,
                    )
                })?;
                ddl_ran = true;
            }
            for col_name in &plan.drop_columns {
                execute_drop_column(&engine, &table_lower, col_name, backend).await?;
                ddl_ran = true;
            }
        }
        warnings.append(&mut plan.warnings);

        crate::log_debug(format!(
            "✅ Ferro Engine: Table '{}' migrated ({} statement(s), {} column(s) dropped)",
            table_lower,
            plan.statements.len(),
            plan.drop_columns.len()
        ));
    }

    if ddl_ran {
        engine.refresh_pool().await.map_err(|e| {
            crate::errors::map_db_error(
                "Auto-migrate applied DDL but failed to refresh the connection pool",
                e,
            )
        })?;
    }

    for warning in &warnings {
        crate::emit_user_warning(warning);
    }

    Ok(())
}

/// The reconciliation pass's label addition (ADR-0011; CONTEXT.md *label
/// addition*): append model-declared labels missing from live ferro-owned
/// enum types. A type is ferro-owned by *derivation* — its name is the one
/// model resolution produces — so the declared side of the diff is itself the
/// ownership test; live types with no model-derived counterpart are user-owned
/// and never touched. Live types absent entirely are the create pass's / ADD
/// COLUMN guard's concern, not label addition's. Returns whether DDL executed.
async fn add_missing_enum_labels(
    engine: &EngineHandle,
    modelset: &IrEnvelope<SchemaIrPayload>,
    warnings: &mut Vec<String>,
) -> PyResult<bool> {
    // Declared native enum types, deduped across models and columns in
    // deterministic order (a shared StrEnum reconciles exactly once).
    let mut declared: std::collections::BTreeMap<String, Vec<String>> = Default::default();
    for model in &modelset.payload.models {
        for col in &model.columns {
            if let Ok(ResolvedStorage::PgEnum { type_name, labels }) =
                resolve_column_storage(col, Dialect::Postgres)
            {
                declared.entry(type_name).or_insert(labels);
            }
        }
    }
    if declared.is_empty() {
        return Ok(false);
    }

    let live = live_enum_type_labels(engine).await?;
    let mut ran = false;
    for (type_name, labels) in &declared {
        let Some(live_labels) = live.get(type_name) else { continue };
        // Warn-never-act (ADR-0011): live labels the model no longer declares
        // are named loudly — rows may still hold them — but never removed.
        // Once per drifted type, not per table referencing it.
        let extra = extra_enum_labels(labels, live_labels);
        if let Some(warning) = extra_enum_labels_warning(type_name, &extra) {
            warnings.push(warning);
        }
        for label in missing_enum_labels(labels, live_labels) {
            let sql = render_pg_enum_add_value(type_name, &label);
            engine.execute_sql_unprepared(&sql).await.map_err(|e| {
                crate::errors::map_db_error(
                    &format!(
                        "Auto-migrate failed to add enum label '{label}' to type '{type_name}'"
                    ),
                    e,
                )
            })?;
            ran = true;
        }
    }
    Ok(ran)
}

/// Manually run the auto-migrate pass against a connected engine.
///
/// Mirrors `connect(auto_migrate=True, migrate_updates=..., migrate_destructive=...)`
/// for consumers that want explicit control over when DDL runs. `updates`
/// defaults to true — calling `migrate()` and getting create-only behavior
/// would be surprising; use `create_tables()` for that.
///
/// On Postgres each table's plan runs in one transaction (a mid-plan failure
/// rolls that table back); SQLite applies statements one at a time.
///
/// # Errors
/// Returns a `PyErr` if the engine is not initialized or the migration fails.
#[pyfunction]
#[pyo3(signature = (using=None, updates=true, destructive=false))]
pub fn migrate(
    py: Python<'_>,
    using: Option<String>,
    updates: bool,
    destructive: bool,
) -> PyResult<Bound<'_, PyAny>> {
    let opts = MigrateOptions::laddered(updates, destructive);
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = engine_for_connection(using)?;
        internal_migrate(engine, opts).await
    })
}

/// Test-only helper: run the migration diff for one table against a JSON
/// description of its live columns, without a database. Returns
/// `(statements, warnings)`; destructive drops are rendered as plain
/// `DROP COLUMN` statements (the dependency-aware index handling needs a live
/// database and is exercised by integration tests).
///
/// # Errors
/// Returns a `PyErr` when the JSON cannot be parsed, the dialect is
/// unrecognized, or the diff contains an unsafe change.
#[pyfunction]
#[pyo3(name = "_render_migration_sql_for_test")]
#[pyo3(signature = (name, schema_ir_json, live_columns_json, dialect, updates=true, destructive=false, live_indexes_json=String::new(), live_foreign_keys_json=String::new(), live_checks_json=String::new()))]
pub fn _render_migration_sql_for_test(
    name: String,
    schema_ir_json: String,
    live_columns_json: String,
    dialect: String,
    updates: bool,
    destructive: bool,
    live_indexes_json: String,
    live_foreign_keys_json: String,
    live_checks_json: String,
) -> PyResult<(Vec<String>, Vec<String>)> {
    let backend = match dialect.as_str() {
        "postgres" => Dialect::Postgres,
        "sqlite" => Dialect::Sqlite,
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Unknown dialect {:?}; expected 'postgres' or 'sqlite'",
                other
            )));
        }
    };
    let declared: IrEnvelope<SchemaIrPayload> = serde_json::from_str(&schema_ir_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("invalid schema_ir_json: {e}"))
    })?;
    let live: Vec<LiveColumn> = serde_json::from_str(&live_columns_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid live-columns JSON: {}", e))
    })?;
    let live_indexes: Vec<LiveIndex> = if live_indexes_json.is_empty() {
        Vec::new()
    } else {
        serde_json::from_str(&live_indexes_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid live-indexes JSON: {}", e))
        })?
    };
    let live_foreign_keys: Vec<LiveForeignKey> = if live_foreign_keys_json.is_empty() {
        Vec::new()
    } else {
        serde_json::from_str(&live_foreign_keys_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "Invalid live-foreign-keys JSON: {}",
                e
            ))
        })?
    };

    let live_checks: Vec<LiveCheck> = if live_checks_json.is_empty() {
        Vec::new()
    } else {
        serde_json::from_str(&live_checks_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid live-checks JSON: {}", e))
        })?
    };

    let table_lower = name;
    let opts = MigrateOptions::laddered(updates, destructive);
    let plan = plan_table_migration(
        &table_lower,
        &declared,
        &live,
        &live_indexes,
        &live_foreign_keys,
        &live_checks,
        backend,
        opts,
    )?;

    let mut statements = plan.statements;
    for col_name in &plan.drop_columns {
        statements.push(format!(
            "ALTER TABLE {} DROP COLUMN {}",
            quote_ident(&table_lower),
            quote_ident(col_name)
        ));
    }
    Ok((statements, plan.warnings))
}
