//! Schema IR diffing and executable SQL emission for runtime migration planning.

pub mod ddl;

use ddl::{
    apply_canonical_type, canonical_type_for_schema_column, literal_default_value,
    pg_alter_type_target, sqlite_declared_type, sqlite_type_class,
};
use ferro_schema_ir::{
    IrEnvelope, SchemaCheck, SchemaColumn, SchemaForeignKey, SchemaIrPayload, SchemaModel,
};
use sea_query::{
    Alias, ColumnDef, ForeignKey, ForeignKeyAction, Index, PostgresQueryBuilder,
    SqliteQueryBuilder, Table,
};
use std::collections::{BTreeMap, BTreeSet};

/// SQL dialect tag for migration emission.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BackendDialect {
    /// SQLite 3.
    Sqlite,
    /// PostgreSQL.
    Postgres,
}

/// One structural change inferred from an IR diff.
#[derive(Clone, Debug, PartialEq)]
pub enum MigrationOp {
    /// A model exists in the new IR but not the old.
    AddTable { model: SchemaModel },
    /// A model was removed — emits `DROP TABLE`.
    DropTable {
        /// Table to drop.
        table: String,
    },
    /// A column exists on the model in the new IR but not in the live/old IR.
    AddColumn {
        table: String,
        column: SchemaColumn,
        foreign_key: Option<SchemaForeignKey>,
        checks: Vec<SchemaCheck>,
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
        table: String,
        old_column: SchemaColumn,
        new_column: SchemaColumn,
    },
    /// `nullable` changed for a column that exists in both snapshots.
    AlterColumnNullability {
        table: String,
        old_column: SchemaColumn,
        new_column: SchemaColumn,
    },
}

/// Ordered migration operations plus non-fatal warnings collected during planning.
#[derive(Clone, Debug, Default, PartialEq)]
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

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct EmittedMigration {
    pub statements: Vec<String>,
    pub drop_columns: Vec<String>,
    pub warnings: Vec<String>,
}

pub fn emit_sql(plan: &MigrationPlan, dialect: BackendDialect) -> Result<EmittedMigration, String> {
    let mut emitted = EmittedMigration::default();
    for operation in &plan.operations {
        match operation {
            MigrationOp::AddTable { model } => {
                let (table_sql, mut trailing) = build_create_table_sqls(model, dialect);
                emitted.statements.push(table_sql);
                emitted.statements.append(&mut trailing);
            }
            MigrationOp::DropTable { table } => {
                emitted.statements.push(format!("DROP TABLE \"{}\"", table));
            }
            MigrationOp::AddColumn {
                table,
                column,
                foreign_key,
                checks,
            } => {
                emit_add_column(
                    &mut emitted,
                    table,
                    column,
                    foreign_key.as_ref(),
                    checks,
                    dialect,
                )?;
            }
            MigrationOp::DropColumn { table, column } => {
                emitted.drop_columns.push(format!("{}::{}", table, column));
            }
            MigrationOp::AlterColumnType {
                table,
                old_column,
                new_column,
            } => emit_alter_type(&mut emitted, table, old_column, new_column, dialect),
            MigrationOp::AlterColumnNullability {
                table,
                old_column,
                new_column,
            } => emit_alter_nullability(&mut emitted, table, old_column, new_column, dialect),
        }
    }
    Ok(emitted)
}

/// Diff two schema IR envelopes and produce a [`MigrationPlan`].
///
/// Compares tables by `table_name` and columns by name within shared tables. Does not
/// diff indexes, FKs, or checks yet — only table/column presence and `db_type` / `nullable`.
///
/// # Arguments
/// * `old_ir` — Baseline schema snapshot.
/// * `new_ir` — Desired schema snapshot.
///
/// # Returns
/// A plan with add/drop/alter operations. Never fails; structural ambiguity is expressed
/// as operations, not errors.
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
        if let Some(model) = new_models.get(*table) {
            plan.operations.push(MigrationOp::AddTable {
                model: (*model).clone(),
            });
        }
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
        let Some(new_col) = new_cols.get(*col) else {
            continue;
        };
        plan.operations.push(MigrationOp::AddColumn {
            table: table.to_string(),
            column: (*new_col).clone(),
            foreign_key: new_model
                .foreign_keys
                .iter()
                .find(|fk| fk.column == *col)
                .cloned(),
            checks: checks_for_column(table, *col, &new_model.checks),
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
        if old_col.primary_key || new_col.primary_key {
            continue;
        }
        if old_col.db_type != new_col.db_type {
            plan.operations.push(MigrationOp::AlterColumnType {
                table: table.to_string(),
                old_column: (*old_col).clone(),
                new_column: (*new_col).clone(),
            });
        }
        if old_col.nullable != new_col.nullable {
            plan.operations.push(MigrationOp::AlterColumnNullability {
                table: table.to_string(),
                old_column: (*old_col).clone(),
                new_column: (*new_col).clone(),
            });
        }
    }
}

fn checks_for_column(table: &str, col: &str, checks: &[SchemaCheck]) -> Vec<SchemaCheck> {
    let suffix = format!("ck_{}_{}", table, col);
    checks
        .iter()
        .filter(|check| check.name == suffix || check.expression.contains(col))
        .cloned()
        .collect()
}

fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

fn single_unique_index_name(table: &str, col: &str) -> String {
    let raw = format!("uq_{}_{}", table, col);
    if raw.chars().count() > 63 {
        return format!("{}_uq", raw.chars().take(60).collect::<String>());
    }
    raw
}

fn fk_action_sql(action: &Option<String>) -> &'static str {
    let upper = action.as_deref().map(str::to_ascii_uppercase);
    match upper.as_deref() {
        Some("RESTRICT") => "RESTRICT",
        Some("SET NULL") => "SET NULL",
        Some("SET DEFAULT") => "SET DEFAULT",
        Some("NO ACTION") => "NO ACTION",
        _ => "CASCADE",
    }
}

fn render_alter(stmt: &sea_query::TableAlterStatement, dialect: BackendDialect) -> String {
    match dialect {
        BackendDialect::Sqlite => stmt.to_string(SqliteQueryBuilder),
        BackendDialect::Postgres => stmt.to_string(PostgresQueryBuilder),
    }
}

fn build_create_table_sqls(model: &SchemaModel, dialect: BackendDialect) -> (String, Vec<String>) {
    let mut table_stmt = Table::create()
        .table(Alias::new(&model.table_name))
        .if_not_exists()
        .to_owned();
    let mut trailing = Vec::new();

    for column in &model.columns {
        let mut col_def = ColumnDef::new(Alias::new(&column.name));
        apply_canonical_type(
            &mut col_def,
            canonical_type_for_schema_column(column, dialect),
        );
        if column.primary_key {
            col_def.primary_key();
            if column.autoincrement {
                col_def.auto_increment();
            }
        }
        if !column.nullable {
            col_def.not_null();
        }
        if column.unique {
            col_def.unique_key();
        }
        table_stmt.col(&mut col_def);
        if column.index {
            let idx_name = format!("idx_{}_{}", model.table_name, column.name);
            let index_stmt = Index::create()
                .name(&idx_name)
                .table(Alias::new(&model.table_name))
                .col(Alias::new(&column.name))
                .if_not_exists()
                .to_owned();
            trailing.push(match dialect {
                BackendDialect::Sqlite => index_stmt.to_string(SqliteQueryBuilder),
                BackendDialect::Postgres => index_stmt.to_string(PostgresQueryBuilder),
            });
        }
    }

    for fk in &model.foreign_keys {
        let on_delete = match fk
            .on_delete
            .as_deref()
            .unwrap_or("CASCADE")
            .to_ascii_uppercase()
            .as_str()
        {
            "RESTRICT" => ForeignKeyAction::Restrict,
            "SET NULL" => ForeignKeyAction::SetNull,
            "SET DEFAULT" => ForeignKeyAction::SetDefault,
            "NO ACTION" => ForeignKeyAction::NoAction,
            _ => ForeignKeyAction::Cascade,
        };
        let mut fk_stmt = ForeignKey::create();
        fk_stmt
            .from(Alias::new(&model.table_name), Alias::new(&fk.column))
            .to(Alias::new(&fk.to_table), Alias::new(&fk.to_column))
            .on_delete(on_delete);
        if let Some(name) = &fk.name {
            fk_stmt.name(name);
        }
        table_stmt.foreign_key(&mut fk_stmt);
    }

    let table_sql = match dialect {
        BackendDialect::Sqlite => table_stmt.build(SqliteQueryBuilder),
        BackendDialect::Postgres => table_stmt.build(PostgresQueryBuilder),
    };
    (table_sql, trailing)
}

fn emit_add_column(
    emitted: &mut EmittedMigration,
    table: &str,
    column: &SchemaColumn,
    foreign_key: Option<&SchemaForeignKey>,
    checks: &[SchemaCheck],
    dialect: BackendDialect,
) -> Result<(), String> {
    if column.primary_key {
        return Err(format!(
            "Cannot add column '{}.{}': it is a primary key, and primary keys cannot be added to existing tables. Use Alembic for this migration.",
            table, column.name
        ));
    }

    let backfill_default = if column.nullable {
        None
    } else {
        let literal = column.default.as_ref().and_then(literal_default_value);
        match literal {
            Some(value) => Some(value),
            None => {
                return Err(format!(
                    "Cannot add NOT NULL column '{}.{}' to an existing table: it has no literal default to backfill existing rows. Make the field nullable, give it a literal default, or use Alembic for this migration.",
                    table, column.name
                ));
            }
        }
    };

    let inline_unique = column.unique && dialect == BackendDialect::Postgres;
    let mut col_def = ColumnDef::new(Alias::new(&column.name));
    apply_canonical_type(
        &mut col_def,
        canonical_type_for_schema_column(column, dialect),
    );
    if !column.nullable {
        col_def.not_null();
    }
    if inline_unique {
        col_def.unique_key();
    }
    if let Some(default) = backfill_default.clone() {
        col_def.default(default);
    }

    let stmt = Table::alter()
        .table(Alias::new(table))
        .add_column(&mut col_def)
        .to_owned();
    emitted.statements.push(render_alter(&stmt, dialect));

    if backfill_default.is_some() && dialect == BackendDialect::Postgres {
        emitted.statements.push(format!(
            "ALTER TABLE {} ALTER COLUMN {} DROP DEFAULT",
            quote_ident(table),
            quote_ident(&column.name)
        ));
    }

    if column.unique && dialect == BackendDialect::Sqlite {
        let index_name = single_unique_index_name(table, &column.name);
        let index_stmt = Index::create()
            .unique()
            .name(&index_name)
            .table(Alias::new(table))
            .col(Alias::new(&column.name))
            .if_not_exists()
            .to_owned();
        emitted
            .statements
            .push(index_stmt.to_string(SqliteQueryBuilder));
        emitted.warnings.push(format!(
            "Added unique column '{}.{}' as a unique index '{}' (SQLite cannot add an inline UNIQUE constraint to an existing table).",
            table, column.name, index_name
        ));
    }

    if column.index {
        let index_name = format!("idx_{}_{}", table, column.name);
        let index_stmt = Index::create()
            .name(&index_name)
            .table(Alias::new(table))
            .col(Alias::new(&column.name))
            .if_not_exists()
            .to_owned();
        emitted.statements.push(match dialect {
            BackendDialect::Sqlite => index_stmt.to_string(SqliteQueryBuilder),
            BackendDialect::Postgres => index_stmt.to_string(PostgresQueryBuilder),
        });
    }

    if let Some(fk) = foreign_key {
        match dialect {
            BackendDialect::Postgres => emitted.statements.push(format!(
                "ALTER TABLE {} ADD FOREIGN KEY ({}) REFERENCES {} ({}) ON DELETE {}",
                quote_ident(table),
                quote_ident(&fk.column),
                quote_ident(&fk.to_table),
                quote_ident(&fk.to_column),
                fk_action_sql(&fk.on_delete)
            )),
            BackendDialect::Sqlite => emitted.warnings.push(format!(
                "Added foreign-key column '{}.{}' without its FOREIGN KEY constraint (SQLite cannot add table constraints to an existing table). Referential integrity for this column is not database-enforced; use Alembic if you need the constraint.",
                table, column.name
            )),
        }
    }

    if dialect == BackendDialect::Postgres {
        for check in checks {
            emitted.statements.push(format!(
                "ALTER TABLE {} ADD CONSTRAINT {} CHECK ({})",
                quote_ident(table),
                quote_ident(&check.name),
                check.expression
            ));
        }
    }

    Ok(())
}

fn emit_alter_type(
    emitted: &mut EmittedMigration,
    table: &str,
    old_col: &SchemaColumn,
    new_col: &SchemaColumn,
    dialect: BackendDialect,
) {
    match dialect {
        BackendDialect::Postgres => {
            if old_col.logical_type == "enum_udt" {
                return;
            }
            let target = pg_alter_type_target(canonical_type_for_schema_column(new_col, dialect));
            emitted.statements.push(format!(
                "ALTER TABLE {} ALTER COLUMN {} TYPE {} USING {}::{}",
                quote_ident(table),
                quote_ident(&new_col.name),
                target,
                quote_ident(&new_col.name),
                target
            ));
        }
        BackendDialect::Sqlite => {
            let old_class = sqlite_type_class(&sqlite_declared_type(
                canonical_type_for_schema_column(old_col, dialect),
            ));
            let new_declared =
                sqlite_declared_type(canonical_type_for_schema_column(new_col, dialect));
            let new_class = sqlite_type_class(&new_declared);
            if old_class != new_class {
                emitted.warnings.push(format!(
                    "Column '{}.{}' type class drifted from '{}' to '{}'. SQLite cannot change column types in place; use Alembic to migrate this column.",
                    table, new_col.name, old_col.db_type, new_col.db_type
                ));
            }
        }
    }
}

fn emit_alter_nullability(
    emitted: &mut EmittedMigration,
    table: &str,
    old_col: &SchemaColumn,
    new_col: &SchemaColumn,
    dialect: BackendDialect,
) {
    match dialect {
        BackendDialect::Postgres => {
            if !new_col.nullable && old_col.nullable {
                emitted.statements.push(format!(
                    "ALTER TABLE {} ALTER COLUMN {} SET NOT NULL",
                    quote_ident(table),
                    quote_ident(&new_col.name)
                ));
            } else if new_col.nullable && !old_col.nullable {
                emitted.statements.push(format!(
                    "ALTER TABLE {} ALTER COLUMN {} DROP NOT NULL",
                    quote_ident(table),
                    quote_ident(&new_col.name)
                ));
            }
        }
        BackendDialect::Sqlite => emitted.warnings.push(format!(
            "Column '{}.{}' nullability drifted from {} to {}. SQLite cannot change column nullability in place; use Alembic to migrate this column.",
            table,
            new_col.name,
            if old_col.nullable { "nullable" } else { "NOT NULL" },
            if new_col.nullable { "nullable" } else { "NOT NULL" }
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ferro_schema_ir::{SchemaCheck, SchemaColumn, SchemaForeignKey, SchemaIndex, SchemaUnique};

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
            vec![
                col("name", "varchar(120)", true),
                col("status", "text", false),
            ],
        ));

        let plan = plan_from_ir(&old_ir, &new_ir);
        assert!(
            plan.operations.iter().any(|op| matches!(
                op,
                MigrationOp::AddColumn { table, column, .. } if table == "doc" && column.name == "status"
            ))
        );
        assert!(plan.operations.contains(&MigrationOp::DropColumn {
            table: "doc".to_string(),
            column: "legacy".to_string()
        }));
        assert!(
            plan.operations.iter().any(|op| matches!(
                op,
                MigrationOp::AlterColumnType { table, new_column, .. } if table == "doc" && new_column.name == "name"
            ))
        );
        assert!(
            plan.operations.iter().any(|op| matches!(
                op,
                MigrationOp::AlterColumnNullability { table, new_column, .. } if table == "doc" && new_column.name == "name"
            ))
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
        let emitted = emit_sql(&plan, BackendDialect::Postgres).unwrap();
        assert_eq!(emitted.drop_columns, vec!["doc::legacy".to_string()]);
    }

    #[test]
    fn emit_sql_add_column_pg_executes() {
        let mut model = schema_model(
            "doc",
            vec![col("id", "int", false), col("slug", "text", true)],
        );
        model.foreign_keys = vec![];
        let plan = MigrationPlan {
            operations: vec![MigrationOp::AddColumn {
                table: "doc".to_string(),
                column: col("slug", "text", true),
                foreign_key: None,
                checks: Vec::new(),
            }],
            warnings: Vec::new(),
        };
        let emitted = emit_sql(&plan, BackendDialect::Postgres).unwrap();
        assert!(emitted.statements[0].contains("ADD COLUMN \"slug\""));
    }

    #[test]
    fn emit_sql_add_column_not_null_without_default_fails() {
        let mut c = col("status", "text", false);
        c.default = None;
        let plan = MigrationPlan {
            operations: vec![MigrationOp::AddColumn {
                table: "doc".to_string(),
                column: c,
                foreign_key: None,
                checks: Vec::new(),
            }],
            warnings: Vec::new(),
        };
        let err = emit_sql(&plan, BackendDialect::Sqlite).unwrap_err();
        assert!(err.contains("literal default"));
    }

    #[test]
    fn emit_sql_alter_nullability_pg() {
        let old = col("name", "text", true);
        let new = col("name", "text", false);
        let plan = MigrationPlan {
            operations: vec![MigrationOp::AlterColumnNullability {
                table: "doc".to_string(),
                old_column: old,
                new_column: new,
            }],
            warnings: Vec::new(),
        };
        let emitted = emit_sql(&plan, BackendDialect::Postgres).unwrap();
        assert_eq!(
            emitted.statements,
            vec!["ALTER TABLE \"doc\" ALTER COLUMN \"name\" SET NOT NULL".to_string()]
        );
    }
}
