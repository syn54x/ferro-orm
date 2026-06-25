//! Unit tests for `ferro-migrate` planning and emission.

use super::*;
use crate::emit::{
    test_composite_index_name, test_composite_unique_index_name, test_db_check_constraint_name,
    test_single_index_name,
};
use ferro_schema_ir::{
    SchemaCheck, SchemaColumn, SchemaForeignKey, SchemaIndex, SchemaUnique,
};

fn envelope(models: Vec<SchemaModel>) -> IrEnvelope<SchemaIrPayload> {
    IrEnvelope {
        ir_kind: "schema".to_string(),
        ir_version: 1,
        payload: SchemaIrPayload {
            dialect_agnostic: true,
            models,
        },
    }
}

fn empty_envelope() -> IrEnvelope<SchemaIrPayload> {
    envelope(Vec::new())
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
        postgres_native_enum: false,
    }
}

fn col_with_flags(
    name: &str,
    db_type: &str,
    nullable: bool,
    unique: bool,
    index: bool,
    default: Option<serde_json::Value>,
) -> SchemaColumn {
    SchemaColumn {
        unique,
        index,
        default,
        ..col(name, db_type, nullable)
    }
}

fn pk_col(name: &str, db_type: &str) -> SchemaColumn {
    SchemaColumn {
        primary_key: true,
        autoincrement: true,
        nullable: false,
        ..col(name, db_type, false)
    }
}

fn assert_no_comment_placeholders(statements: &[String]) {
    for sql in statements {
        assert!(
            !sql.trim_start().starts_with("--"),
            "comment placeholder found: {sql}"
        );
    }
}

#[test]
fn plan_from_ir_detects_add_drop_and_alter_ops() {
    let old_ir = envelope(vec![schema_model(
        "doc",
        vec![col("name", "text", false), col("legacy", "text", true)],
    )]);
    let new_ir = envelope(vec![schema_model(
        "doc",
        vec![col("name", "varchar(120)", true), col("status", "text", false)],
    )]);

    let plan = plan_from_ir(&old_ir, &new_ir);
    assert!(plan.operations.contains(&MigrationOp::AddColumn {
        table: "doc".to_string(),
        column: "status".to_string(),
    }));
    assert!(plan.operations.contains(&MigrationOp::DropColumn {
        table: "doc".to_string(),
        column: "legacy".to_string(),
    }));
    assert!(plan.operations.contains(&MigrationOp::AlterColumnType {
        table: "doc".to_string(),
        column: "name".to_string(),
    }));
    assert!(plan.operations.contains(&MigrationOp::AlterColumnNullability {
        table: "doc".to_string(),
        column: "name".to_string(),
    }));
}

#[test]
fn plan_from_ir_add_and_drop_table() {
    let old_ir = envelope(vec![schema_model("legacy", vec![col("id", "int", false)])]);
    let new_ir = envelope(vec![schema_model("fresh", vec![col("id", "int", false)])]);
    let plan = plan_from_ir(&old_ir, &new_ir);
    assert!(plan.operations.contains(&MigrationOp::AddTable {
        table: "fresh".to_string(),
    }));
    assert!(plan.operations.contains(&MigrationOp::DropTable {
        table: "legacy".to_string(),
    }));
}

#[test]
fn emit_sql_renders_drop_column_postgres() {
    let plan = MigrationPlan {
        operations: vec![MigrationOp::DropColumn {
            table: "doc".to_string(),
            column: "legacy".to_string(),
        }],
        warnings: Vec::new(),
    };
    let sql = emit_sql(&plan, BackendDialect::Postgres);
    assert_eq!(
        sql,
        vec!["ALTER TABLE \"doc\" DROP COLUMN \"legacy\"".to_string()]
    );
}

#[test]
fn emit_sql_renders_drop_column_sqlite() {
    let plan = MigrationPlan {
        operations: vec![MigrationOp::DropColumn {
            table: "doc".to_string(),
            column: "legacy".to_string(),
        }],
        warnings: Vec::new(),
    };
    let sql = emit_sql(&plan, BackendDialect::Sqlite);
    assert_eq!(
        sql,
        vec!["ALTER TABLE \"doc\" DROP COLUMN \"legacy\"".to_string()]
    );
}

#[test]
fn emit_sql_with_ir_drop_table_postgres() {
    let plan = MigrationPlan {
        operations: vec![MigrationOp::DropTable {
            table: "doc".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result = emit_sql_with_ir(&plan, &empty_envelope(), &empty_envelope(), BackendDialect::Postgres)
        .unwrap();
    assert_eq!(result.statements, vec!["DROP TABLE \"doc\"".to_string()]);
    assert_no_comment_placeholders(&result.statements);
}

#[test]
fn emit_sql_with_ir_drop_table_sqlite() {
    let plan = MigrationPlan {
        operations: vec![MigrationOp::DropTable {
            table: "doc".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result = emit_sql_with_ir(&plan, &empty_envelope(), &empty_envelope(), BackendDialect::Sqlite)
        .unwrap();
    assert_eq!(result.statements, vec!["DROP TABLE \"doc\"".to_string()]);
}

#[test]
fn emit_sql_with_ir_add_table_postgres() {
    let model = schema_model(
        "user",
        vec![
            col("id", "int", false),
            col("email", "text", true),
        ],
    );
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddTable {
            table: "user".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &empty_envelope(), &new_ir, BackendDialect::Postgres).unwrap();
    assert!(result.statements[0].contains("CREATE TABLE"));
    assert!(result.statements[0].contains("\"user\""));
    assert_no_comment_placeholders(&result.statements);
}

#[test]
fn emit_sql_with_ir_add_table_sqlite() {
    let model = schema_model("user", vec![col("id", "int", false)]);
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddTable {
            table: "user".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &empty_envelope(), &new_ir, BackendDialect::Sqlite).unwrap();
    assert!(result.statements[0].starts_with("CREATE TABLE"));
}

#[test]
fn emit_sql_with_ir_add_table_unique_inline_sqlite() {
    let model = SchemaModel {
        columns: vec![col_with_flags("email", "text", true, true, false, None)],
        uniques: vec![SchemaUnique {
            name: "uq_user_email".to_string(),
            columns: vec!["email".to_string()],
        }],
        ..schema_model("user", vec![])
    };
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddTable {
            table: "user".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &empty_envelope(), &new_ir, BackendDialect::Sqlite).unwrap();
    assert_eq!(result.statements.len(), 1);
    assert!(result.statements[0].contains("UNIQUE"));
    assert!(!result.statements.iter().any(|s| s.contains("CREATE UNIQUE INDEX")));
}

#[test]
fn emit_sql_with_ir_add_column_nullable_postgres() {
    let model = schema_model("user", vec![col("email", "text", true)]);
    let old_ir = envelope(vec![schema_model("user", vec![col("id", "int", false)])]);
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddColumn {
            table: "user".to_string(),
            column: "email".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Postgres).unwrap();
    assert_eq!(result.statements.len(), 1);
    assert!(result.statements[0].contains("ADD COLUMN"));
    assert!(result.statements[0].contains("email"));
    assert_no_comment_placeholders(&result.statements);
}

#[test]
fn emit_sql_with_ir_add_column_nullable_sqlite() {
    let model = schema_model("user", vec![col("email", "text", true)]);
    let old_ir = envelope(vec![schema_model("user", vec![col("id", "int", false)])]);
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddColumn {
            table: "user".to_string(),
            column: "email".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Sqlite).unwrap();
    assert!(result.statements[0].contains("ADD COLUMN"));
}

#[test]
fn emit_sql_with_ir_add_column_not_null_with_default_postgres() {
    let model = schema_model(
        "user",
        vec![SchemaColumn {
            default: Some(serde_json::json!(0)),
            ..col("score", "int", false)
        }],
    );
    let old_ir = envelope(vec![schema_model("user", vec![])]);
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddColumn {
            table: "user".to_string(),
            column: "score".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Postgres).unwrap();
    assert_eq!(result.statements.len(), 2);
    assert!(result.statements[0].contains("NOT NULL"));
    assert!(result.statements[1].contains("DROP DEFAULT"));
}

#[test]
fn emit_sql_with_ir_add_column_unique_sqlite() {
    let model = schema_model(
        "user",
        vec![col_with_flags("email", "text", true, true, false, None)],
    );
    let old_ir = envelope(vec![schema_model("user", vec![])]);
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddColumn {
            table: "user".to_string(),
            column: "email".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Sqlite).unwrap();
    assert_eq!(result.statements.len(), 2);
    assert!(result.statements[0].contains("ADD COLUMN"));
    assert!(result.statements[1].contains("CREATE UNIQUE INDEX"));
    assert!(result.warnings.iter().any(|w| w.contains("unique index")));
}

#[test]
fn emit_sql_with_ir_add_column_indexed() {
    let model = schema_model(
        "user",
        vec![col_with_flags("email", "text", true, false, true, None)],
    );
    let old_ir = envelope(vec![schema_model("user", vec![])]);
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddColumn {
            table: "user".to_string(),
            column: "email".to_string(),
        }],
        warnings: Vec::new(),
    };
    for dialect in [BackendDialect::Sqlite, BackendDialect::Postgres] {
        let result = emit_sql_with_ir(&plan, &old_ir, &new_ir, dialect).unwrap();
        assert_eq!(result.statements.len(), 2);
        assert!(result.statements[1].contains("CREATE INDEX"));
        assert!(result.statements[1].contains(&test_single_index_name("user", "email")));
    }
}

#[test]
fn emit_sql_with_ir_add_column_fk_postgres() {
    let parent = schema_model("team", vec![col("id", "int", false)]);
    let child = SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            column: "team_id".to_string(),
            to_table: "team".to_string(),
            to_column: "id".to_string(),
            on_delete: Some("CASCADE".to_string()),
            name: Some("fk_user_team_id_team".to_string()),
        }],
        columns: vec![col("team_id", "int", true)],
        ..schema_model("user", vec![])
    };
    let old_ir = envelope(vec![parent.clone(), schema_model("user", vec![])]);
    let new_ir = envelope(vec![parent, child]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddColumn {
            table: "user".to_string(),
            column: "team_id".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Postgres).unwrap();
    assert!(result.statements.iter().any(|s| s.contains("FOREIGN KEY")));
}

#[test]
fn emit_sql_with_ir_add_column_fk_sqlite_warns() {
    let parent = schema_model("team", vec![col("id", "int", false)]);
    let child = SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            column: "team_id".to_string(),
            to_table: "team".to_string(),
            to_column: "id".to_string(),
            on_delete: None,
            name: None,
        }],
        columns: vec![col("team_id", "int", true)],
        ..schema_model("user", vec![])
    };
    let old_ir = envelope(vec![parent.clone(), schema_model("user", vec![])]);
    let new_ir = envelope(vec![parent, child]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddColumn {
            table: "user".to_string(),
            column: "team_id".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Sqlite).unwrap();
    assert!(result.warnings.iter().any(|w| w.contains("FOREIGN KEY")));
}

#[test]
fn emit_sql_with_ir_alter_column_type_postgres() {
    let old_ir = envelope(vec![schema_model("user", vec![col("name", "text", true)])]);
    let new_ir = envelope(vec![schema_model(
        "user",
        vec![col("name", "varchar(120)", true)],
    )]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AlterColumnType {
            table: "user".to_string(),
            column: "name".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Postgres).unwrap();
    assert_eq!(result.statements.len(), 1);
    assert!(result.statements[0].contains("ALTER COLUMN"));
    assert!(result.statements[0].contains("TYPE"));
    assert!(result.statements[0].contains("USING"));
}

#[test]
fn emit_sql_with_ir_alter_column_type_sqlite_warns_only() {
    let old_ir = envelope(vec![schema_model("user", vec![col("name", "text", true)])]);
    let new_ir = envelope(vec![schema_model(
        "user",
        vec![col("name", "int", true)],
    )]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AlterColumnType {
            table: "user".to_string(),
            column: "name".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Sqlite).unwrap();
    assert!(result.statements.is_empty());
    assert!(result.warnings.iter().any(|w| w.contains("cannot change column types")));
}

#[test]
fn emit_sql_with_ir_alter_column_nullability_postgres() {
    let old_ir = envelope(vec![schema_model("user", vec![col("name", "text", true)])]);
    let new_ir = envelope(vec![schema_model("user", vec![col("name", "text", false)])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AlterColumnNullability {
            table: "user".to_string(),
            column: "name".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Postgres).unwrap();
    assert!(result.statements[0].contains("SET NOT NULL"));

    let plan_drop = MigrationPlan {
        operations: vec![MigrationOp::AlterColumnNullability {
            table: "user".to_string(),
            column: "name".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result_drop = emit_sql_with_ir(
        &plan_drop,
        &new_ir,
        &old_ir,
        BackendDialect::Postgres,
    )
    .unwrap();
    assert!(result_drop.statements[0].contains("DROP NOT NULL"));
}

#[test]
fn emit_sql_with_ir_alter_column_nullability_sqlite_warns_only() {
    let old_ir = envelope(vec![schema_model("user", vec![col("name", "text", true)])]);
    let new_ir = envelope(vec![schema_model("user", vec![col("name", "text", false)])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AlterColumnNullability {
            table: "user".to_string(),
            column: "name".to_string(),
        }],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Sqlite).unwrap();
    assert!(result.statements.is_empty());
    assert!(result
        .warnings
        .iter()
        .any(|w| w.contains("cannot change column nullability")));
}

#[test]
fn emit_sql_with_ir_unsafe_not_null_add_errors() {
    let model = schema_model("user", vec![col("score", "int", false)]);
    let old_ir = envelope(vec![schema_model("user", vec![])]);
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddColumn {
            table: "user".to_string(),
            column: "score".to_string(),
        }],
        warnings: Vec::new(),
    };
    let err = emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Postgres).unwrap_err();
    assert!(err.message.contains("NOT NULL"));
    assert!(err.message.contains("no literal default"));
}

#[test]
fn emit_sql_with_ir_drop_primary_key_column_errors() {
    let old_ir = envelope(vec![schema_model("user", vec![pk_col("id", "int"), col("legacy", "text", true)])]);
    let new_ir = envelope(vec![schema_model("user", vec![pk_col("id", "int")])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::DropColumn {
            table: "user".to_string(),
            column: "id".to_string(),
        }],
        warnings: Vec::new(),
    };
    let err = emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Postgres).unwrap_err();
    assert!(err.message.contains("primary key"));
}

#[test]
fn emit_sql_with_ir_add_table_missing_model_errors() {
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddTable {
            table: "missing".to_string(),
        }],
        warnings: Vec::new(),
    };
    let err =
        emit_sql_with_ir(&plan, &empty_envelope(), &empty_envelope(), BackendDialect::Postgres)
            .unwrap_err();
    assert!(err.message.contains("model 'missing' not found"));
}

#[test]
fn emit_sql_with_ir_alter_column_type_unknown_db_type_errors() {
    let bad_col = SchemaColumn {
        db_type: "not_a_real_token".to_string(),
        logical_type: "unknown".to_string(),
        ..col("name", "text", true)
    };
    let old_ir = envelope(vec![schema_model("user", vec![col("name", "text", true)])]);
    let new_ir = envelope(vec![schema_model("user", vec![bad_col])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AlterColumnType {
            table: "user".to_string(),
            column: "name".to_string(),
        }],
        warnings: Vec::new(),
    };
    let err =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Postgres).unwrap_err();
    assert!(err.message.contains("Cannot alter type"));
    assert!(err.message.contains("unknown db_type"));
}

#[test]
fn emit_sql_i1_index_names() {
    assert_eq!(test_single_index_name("user", "email"), "idx_user_email");
    assert_eq!(
        test_composite_index_name("user", &["a", "b"]),
        "idx_user_a_b"
    );
}

#[test]
fn emit_sql_i1_unique_names() {
    assert_eq!(
        test_composite_unique_index_name("user", &["a", "b"]),
        "uq_user_a_b"
    );
}

#[test]
fn emit_sql_i1_check_names() {
    assert_eq!(test_db_check_constraint_name("user", "role"), "ck_user_role");
}

#[test]
fn emit_sql_canonical_db_type_spelling() {
    use ferro_ddl_lowering::{db_type_token_to_canonical, sqlite_declared_type, CanonicalType, Dialect};
    let canonical = db_type_token_to_canonical("date", Dialect::Sqlite).unwrap();
    assert_eq!(canonical, CanonicalType::Date);
    assert_eq!(sqlite_declared_type(canonical), "date_text");
}

#[test]
fn emit_sql_multi_op_ordering() {
    let parent = schema_model("team", vec![col("id", "int", false)]);
    let child = SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            column: "team_id".to_string(),
            to_table: "team".to_string(),
            to_column: "id".to_string(),
            on_delete: None,
            name: None,
        }],
        columns: vec![col("id", "int", false), col("team_id", "int", true)],
        ..schema_model("user", vec![])
    };
    let old_ir = empty_envelope();
    let new_ir = envelope(vec![parent, child]);
    let plan = MigrationPlan {
        operations: vec![
            MigrationOp::AddTable {
                table: "team".to_string(),
            },
            MigrationOp::AddTable {
                table: "user".to_string(),
            },
        ],
        warnings: Vec::new(),
    };
    let result =
        emit_sql_with_ir(&plan, &old_ir, &new_ir, BackendDialect::Postgres).unwrap();
    let create_positions: Vec<usize> = result
        .statements
        .iter()
        .enumerate()
        .filter(|(_, s)| s.starts_with("CREATE TABLE"))
        .map(|(i, _)| i)
        .collect();
    assert_eq!(create_positions.len(), 2);
    let fk_pos = result
        .statements
        .iter()
        .position(|s| s.contains("FOREIGN KEY"))
        .expect("fk statement");
    assert!(fk_pos > create_positions[1]);
}

#[test]
fn emit_sql_no_comment_placeholders_on_full_plan() {
    let old_ir = envelope(vec![schema_model(
        "doc",
        vec![col("name", "text", false)],
    )]);
    let new_ir = envelope(vec![schema_model(
        "doc",
        vec![
            col("name", "varchar(40)", false),
            col("extra", "text", true),
        ],
    )]);
    let plan = plan_from_ir(&old_ir, &new_ir);
    for dialect in [BackendDialect::Sqlite, BackendDialect::Postgres] {
        let result = emit_sql_with_ir(&plan, &old_ir, &new_ir, dialect).unwrap();
        assert_no_comment_placeholders(&result.statements);
    }
}
