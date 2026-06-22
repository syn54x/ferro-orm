use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BackendDialect {
    Sqlite,
    Postgres,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MigrationOp {
    AddTable { table: String },
    DropTable { table: String },
    AddColumn { table: String, column: String },
    DropColumn { table: String, column: String },
    AlterColumnType { table: String, column: String },
    AlterColumnNullability { table: String, column: String },
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct MigrationPlan {
    pub operations: Vec<MigrationOp>,
    pub warnings: Vec<String>,
}

impl MigrationPlan {
    pub fn is_empty(&self) -> bool {
        self.operations.is_empty()
    }
}

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
mod tests {
    use super::*;
    use ferro_schema_ir::{
        SchemaCheck, SchemaColumn, SchemaForeignKey, SchemaIndex, SchemaUnique,
    };

    fn envelope(model: SchemaModel) -> IrEnvelope<SchemaIrPayload> {
        IrEnvelope {
            ir_kind: "schema".to_string(),
            ir_version: 1,
            payload: SchemaIrPayload {
                dialect_agnostic: true,
                models: vec![model],
            },
        }
    }

    fn schema_model(table: &str, cols: Vec<SchemaColumn>) -> SchemaModel {
        SchemaModel {
            model_name: table.to_string(),
            table_name: table.to_string(),
            columns: cols,
            foreign_keys: Vec::<SchemaForeignKey>::new(),
            indexes: Vec::<SchemaIndex>::new(),
            uniques: Vec::<SchemaUnique>::new(),
            checks: Vec::<SchemaCheck>::new(),
        }
    }

    fn col(name: &str, db_type: &str, nullable: bool) -> SchemaColumn {
        SchemaColumn {
            name: name.to_string(),
            logical_type: "string".to_string(),
            db_type: db_type.to_string(),
            db_type_explicit: None,
            nullable,
            primary_key: false,
            autoincrement: false,
            unique: false,
            index: false,
            default: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
        }
    }

    #[test]
    fn plan_from_ir_detects_add_drop_and_alter_ops() {
        let old_ir = envelope(schema_model(
            "doc",
            vec![col("name", "text", false), col("legacy", "text", true)],
        ));
        let new_ir = envelope(schema_model(
            "doc",
            vec![col("name", "varchar(120)", true), col("status", "text", false)],
        ));

        let plan = plan_from_ir(&old_ir, &new_ir);
        assert!(
            plan.operations
                .contains(&MigrationOp::AddColumn { table: "doc".to_string(), column: "status".to_string() })
        );
        assert!(
            plan.operations
                .contains(&MigrationOp::DropColumn { table: "doc".to_string(), column: "legacy".to_string() })
        );
        assert!(
            plan.operations.contains(&MigrationOp::AlterColumnType {
                table: "doc".to_string(),
                column: "name".to_string()
            })
        );
        assert!(
            plan.operations.contains(&MigrationOp::AlterColumnNullability {
                table: "doc".to_string(),
                column: "name".to_string()
            })
        );
    }

    #[test]
    fn emit_sql_renders_drop_column() {
        let plan = MigrationPlan {
            operations: vec![MigrationOp::DropColumn {
                table: "doc".to_string(),
                column: "legacy".to_string(),
            }],
            warnings: Vec::new(),
        };
        let sql = emit_sql(&plan, BackendDialect::Postgres);
        assert_eq!(sql, vec!["ALTER TABLE \"doc\" DROP COLUMN \"legacy\"".to_string()]);
    }
}
