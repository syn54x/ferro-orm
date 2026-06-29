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
        db_type: Some(db_type.to_string()),
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

/// Build a `SchemaColumn` mirroring the IR compiler's output for the golden
/// fixture. `logical_type`/`format`/`db_type` must match what
/// `compile_schema_ir_payload` actually produces (captured empirically).
#[allow(clippy::too_many_arguments)]
fn ir_col(
    name: &str,
    logical_type: &str,
    format: Option<&str>,
    nullable: bool,
    unique: bool,
    index: bool,
) -> SchemaColumn {
    SchemaColumn {
        name: name.to_string(),
        logical_type: logical_type.to_string(),
        db_type: None,
        db_type_explicit: None,
        nullable,
        primary_key: false,
        autoincrement: false,
        unique,
        index,
        default: None,
        format: format.map(str::to_string),
        enum_values: None,
        enum_type_name: None,
        postgres_native_enum: false,
    }
}

/// The comprehensive create-path golden fixture: an `Organization` FK target
/// and an `Account` table exercising every emitted artifact. Column order is
/// alphabetical to mirror the IR compiler (which sorts properties by name).
///
/// Field values (logical_type, format, db_type, the index/unique/check names,
/// FK metadata) were captured from `compile_schema_ir_payload` on the real
/// Ferro models — see `.superpowers/sdd/capture_ground_truth.py`.
fn create_path_golden_fixture() -> Vec<SchemaModel> {
    // The IR emits Optional[int] PKs as nullable=true (the runtime drops NOT NULL
    // for an autoincrement PK regardless); mirror that so the golden byte-matches.
    let organization = schema_model(
        "organization",
        vec![
            SchemaColumn {
                primary_key: true,
                autoincrement: true,
                nullable: true,
                ..ir_col("id", "integer", None, true, false, false)
            },
            col("name", "varchar", false),
        ],
    );

    let role_col = SchemaColumn {
        db_type: Some("text".to_string()),
        db_type_explicit: Some(true),
        enum_values: Some(vec![
            serde_json::json!("admin"),
            serde_json::json!("user"),
        ]),
        enum_type_name: Some("role".to_string()),
        ..ir_col("role", "string", None, false, false, false)
    };

    let account = SchemaModel {
        columns: vec![
            ir_col("avatar", "binary", Some("binary"), false, false, false),
            ir_col("balance", "decimal", Some("decimal"), false, false, false),
            ir_col("birth_date", "date", Some("date"), false, false, false),
            ir_col("created_at", "datetime", Some("date-time"), false, false, false),
            ir_col("email", "string", None, false, false, true),
            // PK id: nullable=true mirrors the IR (Optional[int] PK).
            SchemaColumn {
                primary_key: true,
                autoincrement: true,
                nullable: true,
                ..ir_col("id", "integer", None, true, false, false)
            },
            ir_col("metadata_blob", "json", None, false, false, false),
            ir_col("org_id", "integer", None, false, false, false),
            ir_col("owner_id", "integer", None, false, false, false),
            role_col,
            ir_col("token", "uuid", Some("uuid"), false, false, false),
            ir_col("username", "string", None, false, true, false),
            ir_col("wake_time", "time", Some("time"), false, false, false),
        ],
        foreign_keys: vec![
            SchemaForeignKey {
                column: "org_id".to_string(),
                to_table: "organization".to_string(),
                to_column: "id".to_string(),
                on_delete: Some("CASCADE".to_string()),
                name: Some("fk_account_org_id_organization".to_string()),
            },
            SchemaForeignKey {
                column: "owner_id".to_string(),
                to_table: "organization".to_string(),
                to_column: "id".to_string(),
                on_delete: Some("RESTRICT".to_string()),
                name: Some("fk_account_owner_id_organization".to_string()),
            },
        ],
        indexes: vec![
            SchemaIndex {
                name: "idx_account_created_at_birth_date".to_string(),
                columns: vec!["created_at".to_string(), "birth_date".to_string()],
                unique: false,
            },
            SchemaIndex {
                name: "idx_account_email".to_string(),
                columns: vec!["email".to_string()],
                unique: false,
            },
        ],
        uniques: vec![
            SchemaUnique {
                name: "uq_account_username".to_string(),
                columns: vec!["username".to_string()],
            },
            SchemaUnique {
                name: "uq_account_username_email".to_string(),
                columns: vec!["username".to_string(), "email".to_string()],
            },
        ],
        checks: vec![SchemaCheck {
            name: "ck_account_role".to_string(),
            expression: "role IN ('admin', 'user')".to_string(),
        }],
        ..schema_model("account", vec![])
    };

    vec![organization, account]
}

// Ground truth captured from TODAY's runtime JSON path
// (`ferro._core._render_create_table_sql_for_test`) — see
// `.superpowers/sdd/capture_ground_truth.py`. The new IR-driven
// `render_create_table` must match these byte-for-byte.
const ORG_CREATE_SQLITE: &str =
    "CREATE TABLE IF NOT EXISTS \"organization\" ( \"id\" integer PRIMARY KEY AUTOINCREMENT, \"name\" varchar NOT NULL )";
const ORG_CREATE_POSTGRES: &str =
    "CREATE TABLE IF NOT EXISTS \"organization\" ( \"id\" serial PRIMARY KEY, \"name\" varchar NOT NULL )";
const ACCOUNT_CREATE_SQLITE: &str = "CREATE TABLE IF NOT EXISTS \"account\" ( \"avatar\" blob NOT NULL, \"balance\" real NOT NULL, \"birth_date\" date_text NOT NULL, \"created_at\" timestamp_with_timezone_text NOT NULL, \"email\" varchar NOT NULL, \"id\" integer PRIMARY KEY AUTOINCREMENT, \"metadata_blob\" json_text NOT NULL, \"org_id\" integer NOT NULL, \"owner_id\" integer NOT NULL, \"role\" text NOT NULL, \"token\" uuid_text NOT NULL, \"username\" varchar NOT NULL UNIQUE, \"wake_time\" varchar NOT NULL, FOREIGN KEY (\"org_id\") REFERENCES \"organization\" (\"id\") ON DELETE CASCADE, FOREIGN KEY (\"owner_id\") REFERENCES \"organization\" (\"id\") ON DELETE RESTRICT )";
const ACCOUNT_CREATE_POSTGRES: &str = "CREATE TABLE IF NOT EXISTS \"account\" ( \"avatar\" bytea NOT NULL, \"balance\" decimal NOT NULL, \"birth_date\" date NOT NULL, \"created_at\" timestamp with time zone NOT NULL, \"email\" varchar NOT NULL, \"id\" serial PRIMARY KEY, \"metadata_blob\" json NOT NULL, \"org_id\" integer NOT NULL, \"owner_id\" integer NOT NULL, \"role\" text NOT NULL, \"token\" uuid NOT NULL, \"username\" varchar NOT NULL UNIQUE, \"wake_time\" varchar NOT NULL, FOREIGN KEY (\"org_id\") REFERENCES \"organization\" (\"id\") ON DELETE CASCADE, FOREIGN KEY (\"owner_id\") REFERENCES \"organization\" (\"id\") ON DELETE RESTRICT )";

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

    let plan = plan_from_ir(&old_ir, &new_ir, BackendDialect::Sqlite);
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
    let plan = plan_from_ir(&old_ir, &new_ir, BackendDialect::Sqlite);
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
        db_type: Some("not_a_real_token".to_string()),
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
    assert!(err.message.contains("unknown"));
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
    let create_positions: Vec<(usize, &String)> = result
        .statements
        .iter()
        .enumerate()
        .filter(|(_, s)| s.starts_with("CREATE TABLE"))
        .collect();
    assert_eq!(create_positions.len(), 2);

    // Topo order: the FK target ("team") must be created before the dependent
    // table ("user").
    let team_pos = create_positions
        .iter()
        .position(|(_, s)| s.contains("\"team\""))
        .expect("team create");
    let user_pos = create_positions
        .iter()
        .position(|(_, s)| s.contains("\"user\""))
        .expect("user create");
    assert!(team_pos < user_pos);

    // The FK is now INLINE in the child's CREATE TABLE, not a separate statement.
    let (_, user_create) = create_positions[user_pos];
    assert!(
        user_create.contains("FOREIGN KEY (\"team_id\") REFERENCES \"team\" (\"id\") ON DELETE CASCADE"),
        "inline FK missing from user create: {user_create}"
    );
    assert!(
        !result.statements.iter().any(|s| s.starts_with("ALTER TABLE") && s.contains("FOREIGN KEY")),
        "FKs must be inline, not standalone ALTER statements"
    );
}

#[test]
fn plan_from_ir_adds_missing_index() {
    let old = envelope(vec![schema_model("doc", vec![col("a", "text", true), col("b", "text", true)])]);
    let mut nm = schema_model("doc", vec![col("a", "text", true), col("b", "text", true)]);
    nm.indexes = vec![SchemaIndex { name: "idx_doc_a_b".into(), columns: vec!["a".into(), "b".into()], unique: false }];
    let new = envelope(vec![nm]);
    let plan = plan_from_ir(&old, &new, BackendDialect::Sqlite);
    assert!(plan.operations.contains(&MigrationOp::AddIndex {
        table: "doc".into(), name: "idx_doc_a_b".into(), columns: vec!["a".into(), "b".into()], unique: false
    }));
}

#[test]
fn plan_from_ir_drops_orphaned_index() {
    let mut om = schema_model("doc", vec![col("a", "text", true)]);
    om.indexes = vec![SchemaIndex { name: "idx_doc_a".into(), columns: vec!["a".into()], unique: false }];
    let old = envelope(vec![om]);
    let new = envelope(vec![schema_model("doc", vec![col("a", "text", true)])]); // no index
    let plan = plan_from_ir(&old, &new, BackendDialect::Sqlite);
    assert!(plan.operations.contains(&MigrationOp::DropIndex { table: "doc".into(), name: "idx_doc_a".into() }));
}

#[test]
fn emit_add_index_matches_create_path() {
    let plan = MigrationPlan { operations: vec![MigrationOp::AddIndex {
        table: "doc".into(), name: "idx_doc_a_b".into(), columns: vec!["a".into(), "b".into()], unique: false
    }], warnings: vec![] };
    let r = emit_sql_with_ir(&plan, &empty_envelope(), &empty_envelope(), BackendDialect::Sqlite).unwrap();
    assert_eq!(r.statements, vec!["CREATE INDEX IF NOT EXISTS \"idx_doc_a_b\" ON \"doc\" (\"a\", \"b\")".to_string()]);
}

#[test]
fn emit_drop_index_renders_drop() {
    let plan = MigrationPlan { operations: vec![MigrationOp::DropIndex { table: "doc".into(), name: "idx_doc_a".into() }], warnings: vec![] };
    let r = emit_sql_with_ir(&plan, &empty_envelope(), &empty_envelope(), BackendDialect::Postgres).unwrap();
    assert_eq!(r.statements, vec!["DROP INDEX IF EXISTS \"idx_doc_a\"".to_string()]);
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
    let plan = plan_from_ir(&old_ir, &new_ir, BackendDialect::Sqlite);
    for dialect in [BackendDialect::Sqlite, BackendDialect::Postgres] {
        let result = emit_sql_with_ir(&plan, &old_ir, &new_ir, dialect).unwrap();
        assert_no_comment_placeholders(&result.statements);
    }
}

// Regression: composite index whose columns are all newly added must NOT be skipped.
// Before the fix, `all_columns_are_new` caused this AddIndex to be silently dropped.
#[test]
fn plan_from_ir_composite_all_new_columns_emits_add_index() {
    let old = envelope(vec![schema_model("doc", vec![col("id", "int", false)])]);
    let mut nm = schema_model(
        "doc",
        vec![col("id", "int", false), col("a", "text", true), col("b", "text", true)],
    );
    nm.indexes = vec![SchemaIndex {
        name: "idx_doc_a_b".to_string(),
        columns: vec!["a".to_string(), "b".to_string()],
        unique: false,
    }];
    let new = envelope(vec![nm]);
    let plan = plan_from_ir(&old, &new, BackendDialect::Sqlite);
    assert!(
        plan.operations.contains(&MigrationOp::AddIndex {
            table: "doc".to_string(),
            name: "idx_doc_a_b".to_string(),
            columns: vec!["a".to_string(), "b".to_string()],
            unique: false,
        }),
        "composite index over all-new columns must appear in plan; got: {:?}",
        plan.operations
    );
}

// KTD-2/KTD-3 golden: `render_create_table` must be byte-identical to TODAY's
// runtime JSON path (`build_create_table_sqls`) for the comprehensive fixture,
// emitting FKs INLINE in the CREATE TABLE on BOTH backends.
#[test]
fn render_create_table_golden_sqlite() {
    let models = create_path_golden_fixture();
    let organization = &models[0];
    let account = &models[1];

    let org = render_create_table(organization, BackendDialect::Sqlite).unwrap();
    assert_eq!(org.create_sql, ORG_CREATE_SQLITE);
    assert!(org.post_create_sqls.is_empty());
    assert!(org.warnings.is_empty());

    let acct = render_create_table(account, BackendDialect::Sqlite).unwrap();
    assert_eq!(acct.create_sql, ACCOUNT_CREATE_SQLITE);

    // Post-create order is immaterial and not reconstructable (the IR sorts
    // indexes/uniques by name, discarding declaration order) -> compare sorted.
    let mut got = acct.post_create_sqls.clone();
    got.sort();
    let mut want = vec![
        "CREATE INDEX IF NOT EXISTS \"idx_account_email\" ON \"account\" (\"email\")".to_string(),
        "CREATE UNIQUE INDEX IF NOT EXISTS \"uq_account_username_email\" ON \"account\" (\"username\", \"email\")".to_string(),
        "CREATE INDEX IF NOT EXISTS \"idx_account_created_at_birth_date\" ON \"account\" (\"created_at\", \"birth_date\")".to_string(),
    ];
    want.sort();
    assert_eq!(got, want);

    // SQLite elides the db_check CHECK and warns; no CHECK in any statement.
    assert!(!acct.create_sql.contains("CHECK"));
    assert!(!acct.post_create_sqls.iter().any(|s| s.contains("CHECK")));
    assert!(
        acct.warnings.iter().any(|w| w.contains("ck_account_role")),
        "expected SQLite db_check elision warning, got: {:?}",
        acct.warnings
    );

    // FKs are inline, not in post-create, and no SQLite FK-drop warning.
    assert!(acct.create_sql.contains("FOREIGN KEY (\"org_id\")"));
    assert!(!acct.post_create_sqls.iter().any(|s| s.contains("FOREIGN KEY")));
    assert!(!acct.warnings.iter().any(|w| w.contains("Foreign key")));
}

#[test]
fn render_create_table_golden_postgres() {
    let models = create_path_golden_fixture();
    let organization = &models[0];
    let account = &models[1];

    let org = render_create_table(organization, BackendDialect::Postgres).unwrap();
    assert_eq!(org.create_sql, ORG_CREATE_POSTGRES);
    assert!(org.post_create_sqls.is_empty());

    let acct = render_create_table(account, BackendDialect::Postgres).unwrap();
    assert_eq!(acct.create_sql, ACCOUNT_CREATE_POSTGRES);

    let mut got = acct.post_create_sqls.clone();
    got.sort();
    let mut want = vec![
        "CREATE INDEX IF NOT EXISTS \"idx_account_email\" ON \"account\" (\"email\")".to_string(),
        "ALTER TABLE \"account\" ADD CONSTRAINT \"ck_account_role\" CHECK (\"role\" IN ('admin', 'user'))".to_string(),
        "CREATE UNIQUE INDEX IF NOT EXISTS \"uq_account_username_email\" ON \"account\" (\"username\", \"email\")".to_string(),
        "CREATE INDEX IF NOT EXISTS \"idx_account_created_at_birth_date\" ON \"account\" (\"created_at\", \"birth_date\")".to_string(),
    ];
    want.sort();
    assert_eq!(got, want);

    // Postgres emits the db_check ALTER (quoted column, byte-matching runtime).
    assert!(acct
        .post_create_sqls
        .iter()
        .any(|s| s == "ALTER TABLE \"account\" ADD CONSTRAINT \"ck_account_role\" CHECK (\"role\" IN ('admin', 'user'))"));
    assert!(acct.warnings.is_empty(), "unexpected warnings: {:?}", acct.warnings);

    // FKs inline, not in post-create.
    assert!(acct.create_sql.contains("FOREIGN KEY (\"owner_id\") REFERENCES \"organization\" (\"id\") ON DELETE RESTRICT"));
    assert!(!acct.post_create_sqls.iter().any(|s| s.contains("FOREIGN KEY")));
}

// Verify the runtime's `CASCADE` default (`unwrap_or("CASCADE")`) is mirrored:
// a missing `on_delete` must render `ON DELETE CASCADE`, inline, on both backends.
#[test]
fn render_create_table_fk_none_on_delete_defaults_cascade() {
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
    for dialect in [BackendDialect::Sqlite, BackendDialect::Postgres] {
        let emission = render_create_table(&child, dialect).unwrap();
        assert!(
            emission.create_sql.contains(
                "FOREIGN KEY (\"team_id\") REFERENCES \"team\" (\"id\") ON DELETE CASCADE"
            ),
            "None on_delete must default to CASCADE inline ({dialect:?}): {}",
            emission.create_sql
        );
    }
}

// Regression: single-column index on a newly added column IS correctly skipped
// because emit_add_column emits the CREATE INDEX for that case.
#[test]
fn plan_from_ir_single_column_new_index_is_skipped() {
    let old = envelope(vec![schema_model("doc", vec![col("id", "int", false)])]);
    let mut nm = schema_model(
        "doc",
        vec![col("id", "int", false), col_with_flags("c", "text", true, false, true, None)],
    );
    nm.indexes = vec![SchemaIndex {
        name: "idx_doc_c".to_string(),
        columns: vec!["c".to_string()],
        unique: false,
    }];
    let new = envelope(vec![nm]);
    let plan = plan_from_ir(&old, &new, BackendDialect::Sqlite);
    assert!(
        !plan.operations.contains(&MigrationOp::AddIndex {
            table: "doc".to_string(),
            name: "idx_doc_c".to_string(),
            columns: vec!["c".to_string()],
            unique: false,
        }),
        "single-column index on a new column must NOT appear in plan (emit_add_column handles it); got: {:?}",
        plan.operations
    );
}

// Pin the fail-loud behavior for an unknown logical_type with no db_type.
//
// The Python SchemaIR compiler's `_logical_type` returns `"unknown"` only for
// types that cannot be resolved (unrecognized / None JSON Schema type). Rust's
// `canonical_from_schema_column` → `canonical_from_parts` hits the final `_`
// arm and returns `Err(...)`. This test pins that `render_create_table` surfaces
// the error as `Err(EmissionError)` so a future regression cannot silently
// reintroduce a `unwrap_or(Varchar)` fallback.
//
// This is an "already-green pin" — the behavior holds because
// `canonical_from_parts` has no catch-all fallback; this test makes the
// contract explicit and prevents regression.
#[test]
fn render_create_table_unknown_logical_type_errors() {
    let col = SchemaColumn {
        name: "mystery".to_string(),
        logical_type: "bogus".to_string(),
        db_type: None,
        db_type_explicit: None,
        nullable: true,
        primary_key: false,
        autoincrement: false,
        unique: false,
        index: false,
        default: None,
        format: None,
        enum_values: None,
        enum_type_name: None,
        postgres_native_enum: false,
    };
    let model = schema_model("widget", vec![col]);
    for dialect in [BackendDialect::Sqlite, BackendDialect::Postgres] {
        let result = render_create_table(&model, dialect);
        assert!(
            result.is_err(),
            "render_create_table must fail loud for unknown logical_type (dialect: {dialect:?})"
        );
        let err = result.unwrap_err();
        assert!(
            err.message.contains("bogus"),
            "EmissionError message must identify the offending logical_type token; got: {:?}",
            err.message
        );
    }
}
