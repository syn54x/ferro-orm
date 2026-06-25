//! Schema IR diffing and SQL emission for migration planning.
//!
//! Compares two [`SchemaIrPayload`] snapshots and produces a [`MigrationPlan`].
//! [`emit_sql_with_ir`] lowers structural ops to executable backend-specific DDL.

mod emit;

use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
use std::collections::{BTreeMap, BTreeSet};

pub use emit::emit_sql_with_ir;

/// SQL dialect tag for migration SQL emission.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BackendDialect {
    /// SQLite 3.
    Sqlite,
    /// PostgreSQL.
    Postgres,
}

/// Executable SQL plus non-fatal warnings from [`emit_sql_with_ir`].
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct EmissionResult {
    /// DDL statements to execute in order.
    pub statements: Vec<String>,
    /// Human-readable warnings (backend limitations, skipped alters, …).
    pub warnings: Vec<String>,
}

/// Hard failure during SQL emission (missing IR metadata, unsafe add, …).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EmissionError {
    /// Actionable error message.
    pub message: String,
}

impl std::fmt::Display for EmissionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for EmissionError {}

/// One structural change inferred from an IR diff.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MigrationOp {
    /// A model exists in the new IR but not the old.
    AddTable {
        /// Table to create.
        table: String,
    },
    /// A model was removed — emits `DROP TABLE`.
    DropTable {
        /// Table to drop.
        table: String,
    },
    /// A column exists on the model in the new IR but not in the live/old IR.
    AddColumn {
        /// Owning table.
        table: String,
        /// Column to add.
        column: String,
    },
    /// A column was removed from the model.
    DropColumn {
        /// Owning table.
        table: String,
        /// Column to drop.
        column: String,
    },
    /// `db_type` changed for a column that exists in both snapshots.
    AlterColumnType {
        /// Owning table.
        table: String,
        /// Column whose storage type drifted.
        column: String,
    },
    /// `nullable` changed for a column that exists in both snapshots.
    AlterColumnNullability {
        /// Owning table.
        table: String,
        /// Column whose nullability drifted.
        column: String,
    },
}

/// Ordered migration operations plus non-fatal warnings collected during planning.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct MigrationPlan {
    /// Structural operations to apply (in order).
    pub operations: Vec<MigrationOp>,
    /// Human-readable warnings (e.g. backend limitations) that do not abort planning.
    pub warnings: Vec<String>,
}

impl MigrationPlan {
    /// Returns `true` when there are no operations to run.
    pub fn is_empty(&self) -> bool {
        self.operations.is_empty()
    }
}

/// Render placeholder SQL (or comments) for each operation in `plan`.
///
/// Legacy shim retained until runtime cutover ([#119](https://github.com/syn54x/ferro-orm/issues/119))
/// wires [`emit_sql_with_ir`]. `DropTable` / `DropColumn` are executable; other ops emit comments.
pub fn emit_sql(plan: &MigrationPlan, dialect: BackendDialect) -> Vec<String> {
    let mut sql = Vec::new();
    for operation in &plan.operations {
        match operation {
            MigrationOp::AddTable { table } => {
                sql.push(format!("-- table '{}' must be created via schema emitter", table));
            }
            MigrationOp::DropTable { table } => {
                sql.push(format!("DROP TABLE \"{}\"", table));
            }
            MigrationOp::AddColumn { table, column } => {
                sql.push(format!(
                    "-- column '{}.{}' requires typed ADD COLUMN planning",
                    table, column
                ));
            }
            MigrationOp::DropColumn { table, column } => {
                sql.push(format!(
                    "ALTER TABLE \"{}\" DROP COLUMN \"{}\"",
                    table, column
                ));
            }
            MigrationOp::AlterColumnType { table, column } => match dialect {
                BackendDialect::Postgres => sql.push(format!(
                    "-- alter type for '{}.{}' resolved by backend planner",
                    table, column
                )),
                BackendDialect::Sqlite => sql.push(format!(
                    "-- sqlite cannot alter type in place for '{}.{}'",
                    table, column
                )),
            },
            MigrationOp::AlterColumnNullability { table, column } => match dialect {
                BackendDialect::Postgres => sql.push(format!(
                    "-- alter nullability for '{}.{}' resolved by backend planner",
                    table, column
                )),
                BackendDialect::Sqlite => sql.push(format!(
                    "-- sqlite cannot alter nullability in place for '{}.{}'",
                    table, column
                )),
            },
        }
    }
    sql
}

/// Diff two schema IR envelopes and produce a [`MigrationPlan`].
pub fn plan_from_ir(
    old_ir: &IrEnvelope<SchemaIrPayload>,
    new_ir: &IrEnvelope<SchemaIrPayload>,
) -> MigrationPlan {
    let old_models = index_models(&old_ir.payload.models);
    let new_models = index_models(&new_ir.payload.models);
    let mut plan = MigrationPlan::default();

    let old_tables: BTreeSet<&str> = old_models.keys().map(String::as_str).collect();
    let new_tables: BTreeSet<&str> = new_models.keys().map(String::as_str).collect();

    for table in new_tables.difference(&old_tables) {
        plan.operations.push(MigrationOp::AddTable {
            table: (*table).to_string(),
        });
    }
    for table in old_tables.difference(&new_tables) {
        plan.operations.push(MigrationOp::DropTable {
            table: (*table).to_string(),
        });
    }

    for table in new_tables.intersection(&old_tables) {
        let Some(old_model) = old_models.get(*table) else {
            continue;
        };
        let Some(new_model) = new_models.get(*table) else {
            continue;
        };
        diff_model_columns(*table, old_model, new_model, &mut plan);
    }

    plan
}

fn index_models<'a>(models: &'a [SchemaModel]) -> BTreeMap<String, &'a SchemaModel> {
    let mut indexed = BTreeMap::new();
    for model in models {
        indexed.insert(model.table_name.clone(), model);
    }
    indexed
}

fn diff_model_columns(
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    plan: &mut MigrationPlan,
) {
    let old_cols: BTreeMap<&str, _> = old_model
        .columns
        .iter()
        .map(|column| (column.name.as_str(), column))
        .collect();
    let new_cols: BTreeMap<&str, _> = new_model
        .columns
        .iter()
        .map(|column| (column.name.as_str(), column))
        .collect();

    let old_names: BTreeSet<&str> = old_cols.keys().copied().collect();
    let new_names: BTreeSet<&str> = new_cols.keys().copied().collect();

    for col in new_names.difference(&old_names) {
        plan.operations.push(MigrationOp::AddColumn {
            table: table.to_string(),
            column: (*col).to_string(),
        });
    }
    for col in old_names.difference(&new_names) {
        plan.operations.push(MigrationOp::DropColumn {
            table: table.to_string(),
            column: (*col).to_string(),
        });
    }

    for col in new_names.intersection(&old_names) {
        let Some(old_col) = old_cols.get(*col) else {
            continue;
        };
        let Some(new_col) = new_cols.get(*col) else {
            continue;
        };
        if old_col.db_type != new_col.db_type {
            plan.operations.push(MigrationOp::AlterColumnType {
                table: table.to_string(),
                column: (*col).to_string(),
            });
        }
        if old_col.nullable != new_col.nullable {
            plan.operations.push(MigrationOp::AlterColumnNullability {
                table: table.to_string(),
                column: (*col).to_string(),
            });
        }
    }
}

#[cfg(test)]
mod tests;
