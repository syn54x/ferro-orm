//! Unit tests for `ferro-migrate` planning and emission.

use super::*;
use crate::emit::{
    test_composite_index_name, test_composite_unique_index_name, test_db_check_constraint_name,
    test_single_index_name,
};
use ferro_schema_ir::{
    CheckExpr, IrEnvelope, SchemaCheck, SchemaColumn, SchemaForeignKey, SchemaIndex,
    SchemaIrPayload, SchemaModel, SchemaTableCheck, SchemaUnique,
};

/// The single expected Postgres db_check emission for `account.role IN ('admin','user')`.
/// The bare `ALTER TABLE ... ADD CONSTRAINT` is wrapped in an idempotent DO-block
/// guard (G6, #176) so a re-run against an already-migrated schema is a no-op; the
/// CHECK body stays byte-identical to what the Alembic emitter mirrors.
const PG_DB_CHECK_ACCOUNT_ROLE: &str = "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint \
     WHERE conname = 'ck_account_role' AND conrelid = '\"account\"'::regclass) THEN \
     ALTER TABLE \"account\" ADD CONSTRAINT \"ck_account_role\" \
     CHECK (\"role\" IN ('admin', 'user')); END IF; END $$";

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

/// The plan's statements and warnings flattened in plan order — the shape
/// these emission pins were written against.
fn render_flat(
    plan: &MigrationPlan,
    old_ir: &IrEnvelope<SchemaIrPayload>,
    new_ir: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Result<EmissionResult, EmissionError> {
    let rendered = render_plan(plan, old_ir, new_ir, dialect)?;
    let mut result = EmissionResult {
        statements: Vec::new(),
        warnings: plan.warnings.clone(),
    };
    for op in rendered {
        result.statements.extend(op.statements);
        result.warnings.extend(op.warnings);
    }
    Ok(result)
}

fn empty_envelope() -> IrEnvelope<SchemaIrPayload> {
    envelope(Vec::new())
}

fn schema_model(table: &str, cols: Vec<SchemaColumn>) -> SchemaModel {
    SchemaModel {
        renamed_from: None,
        model_name: table.to_string(),
        table_name: table.to_string(),
        columns: cols,
        foreign_keys: Vec::<SchemaForeignKey>::new(),
        indexes: Vec::<SchemaIndex>::new(),
        uniques: Vec::<SchemaUnique>::new(),
        checks: Vec::<SchemaCheck>::new(),
        table_checks: Vec::new(),
        row_security: None,
    }
}

fn col(name: &str, db_type: &str, nullable: bool) -> SchemaColumn {
    SchemaColumn {
        renamed_from: None,
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
        enum_renamed_labels: Default::default(),
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
        renamed_from: None,
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
        enum_renamed_labels: Default::default(),
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
    // The compiler clamps PK nullability to false (FF-B B5); the emitter skips
    // NOT NULL on PK columns either way, so the golden bytes are unchanged.
    let organization = schema_model(
        "organization",
        vec![
            SchemaColumn {
                primary_key: true,
                autoincrement: true,
                nullable: false,
                ..ir_col("id", "integer", None, false, false, false)
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
            SchemaColumn {
                primary_key: true,
                autoincrement: true,
                nullable: false,
                ..ir_col("id", "integer", None, false, false, false)
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
                renamed_from: None,
                column: "org_id".to_string(),
                to_table: "organization".to_string(),
                to_column: "id".to_string(),
                on_delete: Some("CASCADE".to_string()),
                name: Some("fk_account_org_id_organization".to_string()),
            },
            SchemaForeignKey {
                renamed_from: None,
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
            column: "role".to_string(),
            values: vec!["'admin'".to_string(), "'user'".to_string()],
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
    "CREATE TABLE IF NOT EXISTS \"organization\" ( \"id\" integer NOT NULL PRIMARY KEY AUTOINCREMENT, \"name\" varchar NOT NULL )";
const ORG_CREATE_POSTGRES: &str =
    "CREATE TABLE IF NOT EXISTS \"organization\" ( \"id\" serial PRIMARY KEY NOT NULL, \"name\" varchar NOT NULL )";
const ACCOUNT_CREATE_SQLITE: &str = "CREATE TABLE IF NOT EXISTS \"account\" ( \"avatar\" blob NOT NULL, \"balance\" NUMERIC NOT NULL, \"birth_date\" DATE NOT NULL, \"created_at\" DATETIME NOT NULL, \"email\" varchar NOT NULL, \"id\" integer NOT NULL PRIMARY KEY AUTOINCREMENT, \"metadata_blob\" JSON NOT NULL, \"org_id\" integer NOT NULL, \"owner_id\" integer NOT NULL, \"role\" text NOT NULL CONSTRAINT \"ck_account_role\" CHECK (\"role\" IN ('admin', 'user')), \"token\" CHAR(32) NOT NULL, \"username\" varchar NOT NULL, \"wake_time\" TIME NOT NULL, CONSTRAINT \"fk_account_org_id_organization\" FOREIGN KEY (\"org_id\") REFERENCES \"organization\" (\"id\") ON DELETE CASCADE, CONSTRAINT \"fk_account_owner_id_organization\" FOREIGN KEY (\"owner_id\") REFERENCES \"organization\" (\"id\") ON DELETE RESTRICT )";
const ACCOUNT_CREATE_POSTGRES: &str = "CREATE TABLE IF NOT EXISTS \"account\" ( \"avatar\" bytea NOT NULL, \"balance\" decimal NOT NULL, \"birth_date\" date NOT NULL, \"created_at\" timestamp with time zone NOT NULL, \"email\" varchar NOT NULL, \"id\" serial PRIMARY KEY NOT NULL, \"metadata_blob\" jsonb NOT NULL, \"org_id\" integer NOT NULL, \"owner_id\" integer NOT NULL, \"role\" text NOT NULL, \"token\" uuid NOT NULL, \"username\" varchar NOT NULL, \"wake_time\" time NOT NULL, CONSTRAINT \"fk_account_org_id_organization\" FOREIGN KEY (\"org_id\") REFERENCES \"organization\" (\"id\") ON DELETE CASCADE, CONSTRAINT \"fk_account_owner_id_organization\" FOREIGN KEY (\"owner_id\") REFERENCES \"organization\" (\"id\") ON DELETE RESTRICT )";

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

    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
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

/// A live SQLite `DATETIME` column (introspected token `timestamp`) against a
/// declared `datetime` field (canonical `TimestampTz`, token `timestamptz`):
/// both store as `DATETIME`, so the planner emits no `AlterColumnType` — the
/// phantom `seen has type timestamp, snapshot says timestamptz` drift line.
#[test]
fn plan_from_ir_same_storage_datetime_is_not_a_type_change_on_sqlite() {
    let live = SchemaColumn {
        logical_type: "unknown".to_string(),
        ..col("seen", "timestamp", false)
    };
    let declared = ir_col("seen", "datetime", None, false, false, false);
    let old_ir = envelope(vec![schema_model("author", vec![live])]);
    let new_ir = envelope(vec![schema_model("author", vec![declared])]);

    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
    assert!(
        !plan.operations.contains(&MigrationOp::AlterColumnType {
            table: "author".to_string(),
            column: "seen".to_string(),
        }),
        "same-storage column planned a type change: {:?}",
        plan.operations
    );
}

#[test]
fn plan_from_ir_add_and_drop_table() {
    let old_ir = envelope(vec![schema_model("legacy", vec![col("id", "int", false)])]);
    let new_ir = envelope(vec![schema_model("fresh", vec![col("id", "int", false)])]);
    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
    assert!(plan.operations.contains(&MigrationOp::AddTable {
        table: "fresh".to_string(),
    }));
    assert!(plan.operations.contains(&MigrationOp::DropTable {
        table: "legacy".to_string(),
    }));
}

#[test]
fn render_plan_renders_drop_column_postgres() {
    let old_ir = envelope(vec![schema_model(
        "doc",
        vec![pk_col("id", "int"), col("legacy", "text", true)],
    )]);
    let new_ir = envelope(vec![schema_model("doc", vec![pk_col("id", "int")])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::DropColumn {
            table: "doc".to_string(),
            column: "legacy".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let rendered = render_plan(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(
        rendered[0].statements,
        vec!["ALTER TABLE \"doc\" DROP COLUMN \"legacy\"".to_string()]
    );
}

#[test]
fn render_plan_renders_drop_column_sqlite() {
    let old_ir = envelope(vec![schema_model(
        "doc",
        vec![pk_col("id", "int"), col("legacy", "text", true)],
    )]);
    let new_ir = envelope(vec![schema_model("doc", vec![pk_col("id", "int")])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::DropColumn {
            table: "doc".to_string(),
            column: "legacy".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let rendered = render_plan(&plan, &old_ir, &new_ir, Dialect::Sqlite).unwrap();
    assert_eq!(
        rendered[0].statements,
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(
        &plan,
        &empty_envelope(),
        &empty_envelope(),
        Dialect::Postgres,
    )
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &empty_envelope(), &empty_envelope(), Dialect::Sqlite).unwrap();
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &empty_envelope(), &new_ir, Dialect::Postgres).unwrap();
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &empty_envelope(), &new_ir, Dialect::Sqlite).unwrap();
    assert!(result.statements[0].starts_with("CREATE TABLE"));
}

#[test]
fn emit_sql_with_ir_add_table_single_unique_is_standalone_named_index() {
    // FF-B B4/D1: single-column uniques are standalone named `uq_` unique
    // indexes on both dialects — the one shape fresh-create, the SQLite ALTER
    // path, and Alembic reflection can all agree on. No inline column UNIQUE.
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
        always_warnings: Vec::new(),
    };
    for dialect in [Dialect::Sqlite, Dialect::Postgres] {
        let result = render_flat(&plan, &empty_envelope(), &new_ir, dialect).unwrap();
        assert_eq!(
            result.statements.len(),
            2,
            "{dialect:?}: {:?}",
            result.statements
        );
        assert!(
            !result.statements[0].contains("UNIQUE"),
            "{dialect:?}: no inline UNIQUE in create: {}",
            result.statements[0]
        );
        assert_eq!(
            result.statements[1],
            "CREATE UNIQUE INDEX IF NOT EXISTS \"uq_user_email\" ON \"user\" (\"email\")"
        );
    }
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Sqlite).unwrap();
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(result.statements.len(), 2);
    assert!(result.statements[0].contains("NOT NULL"));
    assert!(result.statements[1].contains("DROP DEFAULT"));
}

#[test]
fn emit_sql_with_ir_add_column_unique_is_standalone_named_index() {
    // FF-B B4/D1: adding a unique column emits the same standalone named
    // `uq_` index shape as fresh create, on both dialects, with no warning
    // (the shape is canonical now, not a SQLite compromise).
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
        always_warnings: Vec::new(),
    };
    for dialect in [Dialect::Sqlite, Dialect::Postgres] {
        let result = render_flat(&plan, &old_ir, &new_ir, dialect).unwrap();
        assert_eq!(
            result.statements.len(),
            2,
            "{dialect:?}: {:?}",
            result.statements
        );
        assert!(result.statements[0].contains("ADD COLUMN"));
        assert!(
            !result.statements[0].contains("UNIQUE"),
            "{dialect:?}: no inline UNIQUE on ADD COLUMN: {}",
            result.statements[0]
        );
        assert_eq!(
            result.statements[1],
            "CREATE UNIQUE INDEX IF NOT EXISTS \"uq_user_email\" ON \"user\" (\"email\")"
        );
        assert!(
            result.warnings.is_empty(),
            "{dialect:?}: unexpected warnings: {:?}",
            result.warnings
        );
    }
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
        always_warnings: Vec::new(),
    };
    for dialect in [Dialect::Sqlite, Dialect::Postgres] {
        let result = render_flat(&plan, &old_ir, &new_ir, dialect).unwrap();
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
            renamed_from: None,
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    // FF-B B4: the added FK carries its IR name via ADD CONSTRAINT.
    assert!(
        result.statements.iter().any(|s| s
            == "ALTER TABLE \"user\" ADD CONSTRAINT \"fk_user_team_id_team\" FOREIGN KEY \
                (\"team_id\") REFERENCES \"team\" (\"id\") ON DELETE CASCADE"),
        "named ADD CONSTRAINT missing: {:?}",
        result.statements
    );
}

#[test]
fn emit_sql_with_ir_add_column_nullable_fk_sqlite_references_inline() {
    let parent = schema_model("team", vec![col("id", "int", false)]);
    let child = SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            renamed_from: None,
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Sqlite).unwrap();
    // A nullable add has no DEFAULT (its default is NULL): the one shape
    // SQLite's ADD COLUMN accepts a REFERENCES clause for (#514). A missing
    // on_delete defaults to CASCADE, as on the create path.
    assert_eq!(
        result.statements,
        vec![
            "ALTER TABLE \"user\" ADD COLUMN \"team_id\" integer \
             REFERENCES \"team\"(\"id\") ON DELETE CASCADE"
                .to_string()
        ]
    );
    assert!(result.warnings.is_empty(), "{:?}", result.warnings);
}

#[test]
fn emit_sql_with_ir_add_column_not_null_fk_with_default_sqlite_warns_naming_migrations() {
    let parent = schema_model("team", vec![col("id", "int", false)]);
    let child = SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            renamed_from: None,
            column: "team_id".to_string(),
            to_table: "team".to_string(),
            to_column: "id".to_string(),
            on_delete: Some("RESTRICT".to_string()),
            name: Some("fk_user_team_id_team".to_string()),
        }],
        columns: vec![SchemaColumn {
            default: Some(serde_json::json!(1)),
            ..col("team_id", "int", false)
        }],
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
        always_warnings: Vec::new(),
    };

    let lite = render_flat(&plan, &old_ir, &new_ir, Dialect::Sqlite).unwrap();
    assert_eq!(
        lite.statements,
        vec!["ALTER TABLE \"user\" ADD COLUMN \"team_id\" integer NOT NULL DEFAULT 1".to_string()],
        "the column is added; SQLite refuses REFERENCES with a non-NULL default"
    );
    assert_eq!(lite.warnings.len(), 1, "{:?}", lite.warnings);
    assert!(
        lite.warnings[0].contains("user.team_id"),
        "{}",
        lite.warnings[0]
    );
    assert!(
        lite.warnings[0].contains("FOREIGN KEY"),
        "{}",
        lite.warnings[0]
    );
    assert!(
        lite.warnings[0].contains("ferro migrate new"),
        "{}",
        lite.warnings[0]
    );
    assert!(
        !lite.warnings[0].contains("Alembic"),
        "{}",
        lite.warnings[0]
    );

    // Postgres: the column, the backfill drop, then the named constraint.
    let pg = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(
        pg.statements,
        vec![
            "ALTER TABLE \"user\" ADD COLUMN \"team_id\" integer NOT NULL DEFAULT 1".to_string(),
            "ALTER TABLE \"user\" ALTER COLUMN \"team_id\" DROP DEFAULT".to_string(),
            "ALTER TABLE \"user\" ADD CONSTRAINT \"fk_user_team_id_team\" FOREIGN KEY \
             (\"team_id\") REFERENCES \"team\" (\"id\") ON DELETE RESTRICT"
                .to_string(),
        ]
    );
    assert!(pg.warnings.is_empty(), "{:?}", pg.warnings);
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Sqlite).unwrap();
    assert!(result.statements.is_empty());
    assert!(result.warnings.iter().any(|w| w.contains("cannot change column types")));
}

#[test]
fn sqlite_warn_skips_name_migrations_not_alembic() {
    // #514: every SQLite warning the pass still emits points at the door that
    // will work — `ferro migrate new` — never at Alembic.
    let type_old = envelope(vec![schema_model("user", vec![col("name", "text", true)])]);
    let type_new = envelope(vec![schema_model("user", vec![col("name", "int", true)])]);
    let null_new = envelope(vec![schema_model("user", vec![col("name", "text", false)])]);
    let fk = SchemaForeignKey {
        renamed_from: None,
        column: "team_id".to_string(),
        to_table: "team".to_string(),
        to_column: "id".to_string(),
        on_delete: Some("CASCADE".to_string()),
        name: Some("fk_user_team_id_team".to_string()),
    };
    let fk_live = envelope(vec![SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            on_delete: Some("RESTRICT".to_string()),
            ..fk.clone()
        }],
        ..schema_model("user", vec![col("team_id", "int", true)])
    }]);
    let fk_new = envelope(vec![SchemaModel {
        foreign_keys: vec![fk],
        ..schema_model("user", vec![col("team_id", "int", true)])
    }]);
    let fk_none = envelope(vec![schema_model(
        "user",
        vec![col("team_id", "int", true)],
    )]);

    let cases: Vec<(
        MigrationOp,
        &IrEnvelope<SchemaIrPayload>,
        &IrEnvelope<SchemaIrPayload>,
    )> = vec![
        (
            MigrationOp::AlterColumnType {
                table: "user".to_string(),
                column: "name".to_string(),
            },
            &type_old,
            &type_new,
        ),
        (
            MigrationOp::AlterColumnNullability {
                table: "user".to_string(),
                column: "name".to_string(),
            },
            &type_old,
            &null_new,
        ),
        (
            MigrationOp::AddForeignKey {
                table: "user".to_string(),
                column: "team_id".to_string(),
            },
            &fk_none,
            &fk_new,
        ),
        (
            MigrationOp::RebuildForeignKey {
                table: "user".to_string(),
                column: "team_id".to_string(),
                old_name: "fk_user_team_id_team".to_string(),
            },
            &fk_live,
            &fk_new,
        ),
    ];
    for (op, old_ir, new_ir) in cases {
        let plan = MigrationPlan {
            operations: vec![op.clone()],
            warnings: Vec::new(),
            always_warnings: Vec::new(),
        };
        let result = render_flat(&plan, old_ir, new_ir, Dialect::Sqlite).unwrap();
        assert!(
            result.statements.is_empty(),
            "{op:?}: {:?}",
            result.statements
        );
        assert_eq!(result.warnings.len(), 1, "{op:?}: {:?}", result.warnings);
        assert!(
            result.warnings[0].contains("ferro migrate new"),
            "{op:?}: {}",
            result.warnings[0]
        );
        assert!(
            !result.warnings[0].contains("Alembic"),
            "{op:?}: {}",
            result.warnings[0]
        );
    }
}

#[test]
fn primary_key_refusals_name_migrations_not_alembic() {
    let with_pk = envelope(vec![schema_model("user", vec![pk_col("id", "int")])]);
    let without = envelope(vec![schema_model("user", vec![])]);
    for (op, old_ir, new_ir) in [
        (
            MigrationOp::AddColumn {
                table: "user".to_string(),
                column: "id".to_string(),
            },
            &without,
            &with_pk,
        ),
        (
            MigrationOp::DropColumn {
                table: "user".to_string(),
                column: "id".to_string(),
            },
            &with_pk,
            &without,
        ),
    ] {
        let plan = MigrationPlan {
            operations: vec![op.clone()],
            warnings: Vec::new(),
            always_warnings: Vec::new(),
        };
        for dialect in [Dialect::Sqlite, Dialect::Postgres] {
            let err = render_flat(&plan, old_ir, new_ir, dialect).unwrap_err();
            assert!(
                err.message.contains("ferro migrate new"),
                "{op:?}: {}",
                err.message
            );
            assert!(!err.message.contains("Alembic"), "{op:?}: {}", err.message);
        }
    }
}

#[test]
fn render_create_table_sqlite_fails_loudly_when_a_column_check_has_no_column() {
    let mut model = schema_model("account", vec![pk_col("id", "int")]);
    model.checks = vec![SchemaCheck {
        name: "ck_account_role".to_string(),
        column: "role".to_string(),
        values: vec!["'admin'".to_string()],
    }];
    let err = render_create_table(&model, Dialect::Sqlite).unwrap_err();
    assert!(err.message.contains("ck_account_role"), "{}", err.message);
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert!(result.statements[0].contains("SET NOT NULL"));

    let plan_drop = MigrationPlan {
        operations: vec![MigrationOp::AlterColumnNullability {
            table: "user".to_string(),
            column: "name".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let result_drop = render_flat(&plan_drop, &new_ir, &old_ir, Dialect::Postgres).unwrap();
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Sqlite).unwrap();
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
        always_warnings: Vec::new(),
    };
    let err = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap_err();
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
        always_warnings: Vec::new(),
    };
    let err = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap_err();
    assert!(err.message.contains("primary key"));
}

#[test]
fn emit_sql_with_ir_add_table_missing_model_errors() {
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddTable {
            table: "missing".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let err = render_flat(
        &plan,
        &empty_envelope(),
        &empty_envelope(),
        Dialect::Postgres,
    )
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
        always_warnings: Vec::new(),
    };
    let err = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap_err();
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
    // SQLAlchemy-compatible SQLite spelling (FF-B B5).
    assert_eq!(sqlite_declared_type(canonical), "DATE");
}

#[test]
fn emit_sql_multi_op_ordering() {
    let parent = schema_model("team", vec![col("id", "int", false)]);
    let child = SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            renamed_from: None,
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
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
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

    // The FK is INLINE in the child's CREATE TABLE, named via the shared
    // fk_name fallback (the fixture sets name: None).
    let (_, user_create) = create_positions[user_pos];
    assert!(
        user_create.contains(
            "CONSTRAINT \"fk_user_team_id_team\" FOREIGN KEY (\"team_id\") REFERENCES \"team\" (\"id\") ON DELETE CASCADE"
        ),
        "named inline FK missing from user create: {user_create}"
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
    let plan = plan_from_ir(
        &old,
        &new,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
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
    let plan = plan_from_ir(
        &old,
        &new,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
    assert!(plan.operations.contains(&MigrationOp::DropIndex {
        table: "doc".into(),
        name: "idx_doc_a".into()
    }));
}

#[test]
fn emit_add_index_matches_create_path() {
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddIndex {
            table: "doc".into(),
            name: "idx_doc_a_b".into(),
            columns: vec!["a".into(), "b".into()],
            unique: false,
        }],
        warnings: vec![],
        always_warnings: Vec::new(),
    };
    let r = render_flat(&plan, &empty_envelope(), &empty_envelope(), Dialect::Sqlite).unwrap();
    assert_eq!(
        r.statements,
        vec!["CREATE INDEX IF NOT EXISTS \"idx_doc_a_b\" ON \"doc\" (\"a\", \"b\")".to_string()]
    );
}

#[test]
fn emit_drop_index_renders_drop() {
    let plan = MigrationPlan {
        operations: vec![MigrationOp::DropIndex {
            table: "doc".into(),
            name: "idx_doc_a".into(),
        }],
        warnings: vec![],
        always_warnings: Vec::new(),
    };
    let r = render_flat(
        &plan,
        &empty_envelope(),
        &empty_envelope(),
        Dialect::Postgres,
    )
    .unwrap();
    assert_eq!(
        r.statements,
        vec!["DROP INDEX IF EXISTS \"idx_doc_a\"".to_string()]
    );
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
    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
    for dialect in [Dialect::Sqlite, Dialect::Postgres] {
        let result = render_flat(&plan, &old_ir, &new_ir, dialect).unwrap();
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
    let plan = plan_from_ir(
        &old,
        &new,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
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

#[test]
fn emit_alter_type_refuses_timestamp_to_timestamptz_on_postgres() {
    // Live column is naive `timestamp`; the model maps `datetime` -> timestamptz.
    let old_ir = envelope(vec![schema_model(
        "event",
        vec![col("occurred_at", "timestamp", false)],
    )]);
    let new_ir = envelope(vec![schema_model(
        "event",
        vec![ir_col("occurred_at", "datetime", None, false, false, false)],
    )]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AlterColumnType {
            table: "event".to_string(),
            column: "occurred_at".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert!(
        result.statements.is_empty(),
        "expected no ALTER statement, got {:?}",
        result.statements
    );
    assert_eq!(result.warnings.len(), 1);
    assert!(result.warnings[0].contains("occurred_at"));
    assert!(result.warnings[0].contains("db_type"));
    assert!(result.warnings[0].contains("Alembic"));
}

#[test]
fn emit_alter_type_still_alters_int_to_bigint_on_postgres() {
    let old_ir = envelope(vec![schema_model("m", vec![col("n", "int", false)])]);
    let new_ir = envelope(vec![schema_model("m", vec![col("n", "bigint", false)])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AlterColumnType {
            table: "m".to_string(),
            column: "n".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(result.statements.len(), 1);
    assert!(result.statements[0].contains("ALTER COLUMN"));
    assert!(result.warnings.is_empty());
}

// KTD-2/KTD-3 golden: `render_create_table` must be byte-identical to TODAY's
// runtime JSON path (`build_create_table_sqls`) for the comprehensive fixture,
// emitting FKs INLINE in the CREATE TABLE on BOTH backends.
#[test]
fn render_create_table_golden_sqlite() {
    let models = create_path_golden_fixture();
    let organization = &models[0];
    let account = &models[1];

    let org = render_create_table(organization, Dialect::Sqlite).unwrap();
    assert_eq!(org.create_sql, ORG_CREATE_SQLITE);
    assert!(org.post_create_sqls.is_empty());
    assert!(org.warnings.is_empty());

    let acct = render_create_table(account, Dialect::Sqlite).unwrap();
    assert_eq!(acct.create_sql, ACCOUNT_CREATE_SQLITE);

    // Post-create order is immaterial and not reconstructable (the IR sorts
    // indexes/uniques by name, discarding declaration order) -> compare sorted.
    let mut got = acct.post_create_sqls.clone();
    got.sort();
    let mut want = vec![
        "CREATE INDEX IF NOT EXISTS \"idx_account_email\" ON \"account\" (\"email\")".to_string(),
        "CREATE UNIQUE INDEX IF NOT EXISTS \"uq_account_username\" ON \"account\" (\"username\")".to_string(),
        "CREATE UNIQUE INDEX IF NOT EXISTS \"uq_account_username_email\" ON \"account\" (\"username\", \"email\")".to_string(),
        "CREATE INDEX IF NOT EXISTS \"idx_account_created_at_birth_date\" ON \"account\" (\"created_at\", \"birth_date\")".to_string(),
    ];
    want.sort();
    assert_eq!(got, want);

    // SQLite carries the db_check inline on its column (#514): named, in the
    // CREATE TABLE, never a post-create statement, and no elision warning.
    assert!(acct.create_sql.contains(
        "\"role\" text NOT NULL CONSTRAINT \"ck_account_role\" CHECK (\"role\" IN ('admin', 'user'))"
    ));
    assert!(!acct.post_create_sqls.iter().any(|s| s.contains("CHECK")));
    assert!(
        acct.warnings.is_empty(),
        "unexpected warnings: {:?}",
        acct.warnings
    );

    // FKs are inline, named, not in post-create, and no SQLite FK-drop warning.
    assert!(
        acct.create_sql
            .contains("CONSTRAINT \"fk_account_org_id_organization\" FOREIGN KEY (\"org_id\")")
    );
    assert!(
        !acct
            .post_create_sqls
            .iter()
            .any(|s| s.contains("FOREIGN KEY"))
    );
    assert!(!acct.warnings.iter().any(|w| w.contains("Foreign key")));
}

#[test]
fn render_create_table_golden_postgres() {
    let models = create_path_golden_fixture();
    let organization = &models[0];
    let account = &models[1];

    let org = render_create_table(organization, Dialect::Postgres).unwrap();
    assert_eq!(org.create_sql, ORG_CREATE_POSTGRES);
    assert!(org.post_create_sqls.is_empty());

    let acct = render_create_table(account, Dialect::Postgres).unwrap();
    assert_eq!(acct.create_sql, ACCOUNT_CREATE_POSTGRES);

    let mut got = acct.post_create_sqls.clone();
    got.sort();
    let mut want = vec![
        "CREATE INDEX IF NOT EXISTS \"idx_account_email\" ON \"account\" (\"email\")".to_string(),
        PG_DB_CHECK_ACCOUNT_ROLE.to_string(),
        "CREATE UNIQUE INDEX IF NOT EXISTS \"uq_account_username\" ON \"account\" (\"username\")".to_string(),
        "CREATE UNIQUE INDEX IF NOT EXISTS \"uq_account_username_email\" ON \"account\" (\"username\", \"email\")".to_string(),
        "CREATE INDEX IF NOT EXISTS \"idx_account_created_at_birth_date\" ON \"account\" (\"created_at\", \"birth_date\")".to_string(),
    ];
    want.sort();
    assert_eq!(got, want);

    // Postgres emits the db_check ALTER (quoted column, byte-matching runtime),
    // wrapped in the idempotent DO-block guard (G6, #176).
    assert!(acct
        .post_create_sqls
        .iter()
        .any(|s| s == PG_DB_CHECK_ACCOUNT_ROLE));
    assert!(acct.warnings.is_empty(), "unexpected warnings: {:?}", acct.warnings);

    // FKs inline, named, not in post-create.
    assert!(acct.create_sql.contains("CONSTRAINT \"fk_account_owner_id_organization\" FOREIGN KEY (\"owner_id\") REFERENCES \"organization\" (\"id\") ON DELETE RESTRICT"));
    assert!(!acct.post_create_sqls.iter().any(|s| s.contains("FOREIGN KEY")));
}

/// The pinned CREATE TYPE guard for the golden enum fixture.
const PG_CREATE_TYPE_STATUS: &str = "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type t \
     JOIN pg_namespace n ON n.oid = t.typnamespace \
     WHERE t.typname = 'status' AND n.nspname = current_schema()) THEN \
     CREATE TYPE \"status\" AS ENUM ('draft', 'archived'); \
     END IF; END $$";

fn enum_fixture_model() -> SchemaModel {
    let status_col = SchemaColumn {
        enum_values: Some(vec![
            serde_json::json!("draft"),
            serde_json::json!("archived"),
        ]),
        enum_type_name: Some("status".to_string()),
        db_type: None,
        ..col("status", "text", false)
    };
    schema_model("ticket", vec![pk_col("id", "int"), status_col])
}

// FF-B B2: enum columns (no explicit db_type) lower to a native Postgres enum
// — an idempotent CREATE TYPE guard in pre-create plus a column typed as the
// enum — and to varchar(max label len) on SQLite, matching what SQLAlchemy
// renders for sa.Enum on each backend.
#[test]
fn render_create_table_enum_native_on_postgres() {
    let model = enum_fixture_model();
    let emission = render_create_table(&model, Dialect::Postgres).unwrap();
    assert_eq!(emission.pre_create_sqls, vec![PG_CREATE_TYPE_STATUS.to_string()]);
    assert!(
        emission
            .create_sql
            .contains("\"status\" status NOT NULL"),
        "column must be typed as the enum: {}",
        emission.create_sql
    );
}

#[test]
fn render_create_table_enum_varchar_on_sqlite() {
    let model = enum_fixture_model();
    let emission = render_create_table(&model, Dialect::Sqlite).unwrap();
    assert!(emission.pre_create_sqls.is_empty());
    assert!(
        emission
            .create_sql
            .contains("\"status\" varchar(8) NOT NULL"),
        "sa.Enum renders VARCHAR(max label len) on SQLite: {}",
        emission.create_sql
    );
}

#[test]
fn emit_add_table_dedupes_create_type_across_columns() {
    // Two columns sharing one enum type must emit exactly one CREATE TYPE guard.
    let status = |name: &str| SchemaColumn {
        enum_values: Some(vec![
            serde_json::json!("draft"),
            serde_json::json!("archived"),
        ]),
        enum_type_name: Some("status".to_string()),
        db_type: None,
        ..col(name, "text", false)
    };
    let model = schema_model(
        "ticket",
        vec![pk_col("id", "int"), status("state_a"), status("state_b")],
    );
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddTable {
            table: "ticket".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &empty_envelope(), &new_ir, Dialect::Postgres).unwrap();
    let create_types: Vec<&String> = result
        .statements
        .iter()
        .filter(|s| s.contains("CREATE TYPE"))
        .collect();
    assert_eq!(create_types.len(), 1, "{:?}", result.statements);
    // And the guard precedes the CREATE TABLE.
    let type_pos = result.statements.iter().position(|s| s.contains("CREATE TYPE"));
    let table_pos = result.statements.iter().position(|s| s.starts_with("CREATE TABLE"));
    assert!(type_pos < table_pos);
}

#[test]
fn emit_add_column_enum_postgres_creates_type_then_column() {
    // Nullable so the add needs no backfill default.
    let mut model = enum_fixture_model();
    for c in &mut model.columns {
        if c.name == "status" {
            c.nullable = true;
        }
    }
    let old_ir = envelope(vec![schema_model("ticket", vec![pk_col("id", "int")])]);
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddColumn {
            table: "ticket".to_string(),
            column: "status".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(result.statements[0], PG_CREATE_TYPE_STATUS);
    assert!(
        result.statements[1].contains("ADD COLUMN \"status\" status"),
        "{:?}",
        result.statements
    );

    let sqlite = render_flat(&plan, &old_ir, &new_ir, Dialect::Sqlite).unwrap();
    assert!(
        sqlite.statements[0].contains("ADD COLUMN \"status\" varchar(8)"),
        "{:?}",
        sqlite.statements
    );
    assert!(!sqlite.statements.iter().any(|s| s.contains("CREATE TYPE")));
}

#[test]
fn emit_alter_refuses_varchar_to_enum_and_varchar_to_time() {
    // Live varchar columns (old lowering) targeted at native enum / time are
    // REFUSED: warning, no ALTER (the #154 pattern generalized).
    let live_enum_col = col("status", "varchar", false);
    let live_time_col = col("wake_time", "varchar", false);
    let model = SchemaModel {
        columns: vec![
            pk_col("id", "int"),
            SchemaColumn {
                enum_values: Some(vec![serde_json::json!("draft")]),
                enum_type_name: Some("status".to_string()),
                db_type: None,
                ..col("status", "text", false)
            },
            ir_col("wake_time", "time", Some("time"), false, false, false),
        ],
        ..schema_model("ticket", vec![])
    };
    let old_ir = envelope(vec![schema_model(
        "ticket",
        vec![pk_col("id", "int"), live_enum_col, live_time_col],
    )]);
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![
            MigrationOp::AlterColumnType {
                table: "ticket".to_string(),
                column: "status".to_string(),
            },
            MigrationOp::AlterColumnType {
                table: "ticket".to_string(),
                column: "wake_time".to_string(),
            },
        ],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert!(result.statements.is_empty(), "{:?}", result.statements);
    assert_eq!(result.warnings.len(), 2, "{:?}", result.warnings);
    assert!(result.warnings[0].contains("ticket.status"), "{}", result.warnings[0]);
    assert!(result.warnings[0].contains("USING"), "{}", result.warnings[0]);
    assert!(result.warnings[1].contains("ticket.wake_time"), "{}", result.warnings[1]);
}

#[test]
fn emit_alter_native_enum_live_is_noop() {
    let live = SchemaColumn {
        postgres_native_enum: true,
        enum_renamed_labels: Default::default(),
        ..col("status", "varchar", false)
    };
    let model_col = SchemaColumn {
        enum_values: Some(vec![serde_json::json!("draft")]),
        enum_type_name: Some("status".to_string()),
        db_type: None,
        ..col("status", "text", false)
    };
    let old_ir = envelope(vec![schema_model("ticket", vec![live])]);
    let new_ir = envelope(vec![schema_model("ticket", vec![model_col])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AlterColumnType {
            table: "ticket".to_string(),
            column: "status".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert!(result.statements.is_empty());
    assert!(result.warnings.is_empty());
}

// Verify the runtime's `CASCADE` default (`unwrap_or("CASCADE")`) is mirrored:
// a missing `on_delete` must render `ON DELETE CASCADE`, inline, on both backends.
#[test]
fn render_create_table_fk_none_on_delete_defaults_cascade() {
    let child = SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            renamed_from: None,
            column: "team_id".to_string(),
            to_table: "team".to_string(),
            to_column: "id".to_string(),
            on_delete: None,
            name: None,
        }],
        columns: vec![col("team_id", "int", true)],
        ..schema_model("user", vec![])
    };
    for dialect in [Dialect::Sqlite, Dialect::Postgres] {
        let emission = render_create_table(&child, dialect).unwrap();
        assert!(
            emission.create_sql.contains(
                "CONSTRAINT \"fk_user_team_id_team\" FOREIGN KEY (\"team_id\") REFERENCES \"team\" (\"id\") ON DELETE CASCADE"
            ),
            "None on_delete must default to CASCADE inline with the fallback fk_ name ({dialect:?}): {}",
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
    let plan = plan_from_ir(
        &old,
        &new,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
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
        renamed_from: None,
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
        enum_renamed_labels: Default::default(),
    };
    let model = schema_model("widget", vec![col]);
    for dialect in [Dialect::Sqlite, Dialect::Postgres] {
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

/// The Transfer-shaped table check: `outflow_transaction_id IS NULL OR
/// outflow_activity_id IS NULL`, under the canonical `ck_<table>_<suffix>` name.
fn transfer_outflow_table_check() -> SchemaTableCheck {
    SchemaTableCheck {
        name: ferro_ddl_lowering::table_check_constraint_name("transfer", "at_most_one_outflow"),
        predicate: CheckExpr::Or {
            left: Box::new(CheckExpr::IsNull {
                column: "outflow_transaction_id".to_string(),
            }),
            right: Box::new(CheckExpr::IsNull {
                column: "outflow_activity_id".to_string(),
            }),
        },
    }
}

fn transfer_inflow_table_check() -> SchemaTableCheck {
    SchemaTableCheck {
        name: ferro_ddl_lowering::table_check_constraint_name("transfer", "at_most_one_inflow"),
        predicate: CheckExpr::Or {
            left: Box::new(CheckExpr::IsNull {
                column: "inflow_transaction_id".to_string(),
            }),
            right: Box::new(CheckExpr::IsNull {
                column: "inflow_activity_id".to_string(),
            }),
        },
    }
}

fn transfer_model_with_table_checks(checks: Vec<SchemaTableCheck>) -> SchemaModel {
    SchemaModel {
        table_checks: checks,
        ..schema_model(
            "transfer",
            vec![
                pk_col("id", "int"),
                col("inflow_activity_id", "int", true),
                col("inflow_transaction_id", "int", true),
                col("outflow_activity_id", "int", true),
                col("outflow_transaction_id", "int", true),
            ],
        )
    }
}

// A table check is INLINE in CREATE TABLE on BOTH dialects (ADR-0014): SQLite
// can represent it at create time, and the ALTER-shaped `post_create_artifacts`
// path (which elides column `db_check` on SQLite) is never involved.
#[test]
fn render_create_table_inlines_named_table_checks_on_both_dialects() {
    let model = transfer_model_with_table_checks(vec![transfer_outflow_table_check()]);
    let expected = "CONSTRAINT \"ck_transfer_at_most_one_outflow\" CHECK \
                    ((\"outflow_transaction_id\" IS NULL) OR (\"outflow_activity_id\" IS NULL))";
    for dialect in [Dialect::Sqlite, Dialect::Postgres] {
        let emission = render_create_table(&model, dialect).unwrap();
        assert!(
            emission.create_sql.contains(expected),
            "table check must be inline and named in CREATE TABLE ({dialect:?}): {}",
            emission.create_sql
        );
        assert!(
            emission.create_sql.ends_with(" )"),
            "the spliced constraint list must still close the CREATE TABLE ({dialect:?}): {}",
            emission.create_sql
        );
        assert!(
            !emission.post_create_sqls.iter().any(|s| s.contains("CHECK")),
            "table checks must not travel the ALTER-shaped post-create path ({dialect:?}): {:?}",
            emission.post_create_sqls
        );
        assert!(emission.warnings.is_empty(), "{:?}", emission.warnings);
    }
}

// Several checks keep IR order and are comma-separated like every other
// inline constraint.
#[test]
fn render_create_table_inlines_every_table_check_in_ir_order() {
    let model = transfer_model_with_table_checks(vec![
        transfer_outflow_table_check(),
        transfer_inflow_table_check(),
    ]);
    for dialect in [Dialect::Sqlite, Dialect::Postgres] {
        let sql = render_create_table(&model, dialect).unwrap().create_sql;
        let outflow = sql
            .find("ck_transfer_at_most_one_outflow")
            .unwrap_or_else(|| panic!("missing outflow check ({dialect:?}): {sql}"));
        let inflow = sql
            .find("ck_transfer_at_most_one_inflow")
            .unwrap_or_else(|| panic!("missing inflow check ({dialect:?}): {sql}"));
        assert!(outflow < inflow, "IR order must be preserved ({dialect:?}): {sql}");
        assert!(
            sql.contains(
                "IS NULL)), CONSTRAINT \"ck_transfer_at_most_one_inflow\""
            ),
            "checks must be comma-separated inline clauses ({dialect:?}): {sql}"
        );
    }
}

// A model with no table checks renders byte-identically to before (the splice
// is a no-op, not an empty trailing comma).
#[test]
fn render_create_table_without_table_checks_is_unchanged() {
    let with_checks = transfer_model_with_table_checks(vec![]);
    for dialect in [Dialect::Sqlite, Dialect::Postgres] {
        let sql = render_create_table(&with_checks, dialect).unwrap().create_sql;
        assert!(!sql.contains("CHECK"), "{sql}");
        assert!(!sql.contains(", )"), "{sql}");
    }
}

// ---------------------------------------------------------------------------
// Check addition (#343; ADR-0013): the reconciliation pass adds a declared
// CHECK — table check or column check — that no live constraint of that name
// covers. Body drift is a rebuild (#344) and orphan drops are #345.
// ---------------------------------------------------------------------------

/// The live IR shape the reconciliation pass builds for `transfer` before the
/// check was declared: the same columns, no checks (live CHECK names travel
/// beside the IR, not inside it — a live `definition` is the backend's own SQL
/// rendering, never a ferro body).
fn live_transfer_ir() -> IrEnvelope<SchemaIrPayload> {
    envelope(vec![transfer_model_with_table_checks(vec![])])
}

#[test]
fn plan_missing_checks_adds_a_declared_table_check_absent_live() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![
        transfer_outflow_table_check(),
    ])]);
    assert_eq!(
        plan_missing_checks("transfer", &live_transfer_ir(), &new_ir, &[]),
        vec![MigrationOp::AddCheck {
            table: "transfer".to_string(),
            name: "ck_transfer_at_most_one_outflow".to_string(),
        }]
    );
}

#[test]
fn plan_missing_checks_is_a_noop_when_the_live_table_already_has_the_name() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![
        transfer_outflow_table_check(),
    ])]);
    assert!(
        plan_missing_checks(
            "transfer",
            &live_transfer_ir(),
            &new_ir,
            &["ck_transfer_at_most_one_outflow".to_string()],
        )
        .is_empty(),
        "a reconciled table replans to nothing — no phantom add"
    );
}

#[test]
fn plan_missing_checks_skips_a_column_check_riding_its_new_column() {
    // `emit_add_column` already emits the db_check DO-block for a column it
    // adds; a standalone AddCheck would duplicate it (the same dedup
    // `diff_model_indexes` applies to single-column indexes).
    let mut model = schema_model("account", vec![ir_col("role", "string", None, true, false, false)]);
    model.checks = vec![SchemaCheck {
        name: test_db_check_constraint_name("account", "role"),
        column: "role".to_string(),
        values: vec!["'admin'".to_string()],
    }];
    let new_ir = envelope(vec![model.clone()]);

    let without_column = envelope(vec![schema_model("account", vec![])]);
    assert!(
        plan_missing_checks("account", &without_column, &new_ir, &[]).is_empty(),
        "the check rides the ADD COLUMN emission"
    );

    let with_column = envelope(vec![schema_model(
        "account",
        vec![ir_col("role", "string", None, true, false, false)],
    )]);
    assert_eq!(
        plan_missing_checks("account", &with_column, &new_ir, &[]),
        vec![MigrationOp::AddCheck {
            table: "account".to_string(),
            name: "ck_account_role".to_string(),
        }],
        "toggling db_check on an EXISTING column is a standalone add"
    );
}

#[test]
fn emit_sql_with_ir_add_check_table_check_alters_on_postgres_and_warns_on_sqlite() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![
        transfer_outflow_table_check(),
    ])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddCheck {
            table: "transfer".to_string(),
            name: "ck_transfer_at_most_one_outflow".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };

    let pg = render_flat(&plan, &live_transfer_ir(), &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(
        pg.statements,
        vec![
            "ALTER TABLE \"transfer\" ADD CONSTRAINT \"ck_transfer_at_most_one_outflow\" \
             CHECK ((\"outflow_transaction_id\" IS NULL) OR (\"outflow_activity_id\" IS NULL))"
        ]
    );
    assert!(pg.warnings.is_empty(), "{:?}", pg.warnings);

    let lite = render_flat(&plan, &live_transfer_ir(), &new_ir, Dialect::Sqlite).unwrap();
    assert!(lite.statements.is_empty(), "{:?}", lite.statements);
    assert_eq!(lite.warnings.len(), 1);
    assert!(
        lite.warnings[0].contains("ck_transfer_at_most_one_outflow"),
        "the SQLite skip names the constraint: {}",
        lite.warnings[0]
    );
}

#[test]
fn emit_sql_with_ir_add_check_column_check_reuses_the_db_check_do_block() {
    let mut model = schema_model("account", vec![ir_col("role", "string", None, true, false, false)]);
    model.checks = vec![SchemaCheck {
        name: test_db_check_constraint_name("account", "role"),
        column: "role".to_string(),
        values: vec!["'admin'".to_string(), "'user'".to_string()],
    }];
    let old_ir = envelope(vec![schema_model(
        "account",
        vec![ir_col("role", "string", None, true, false, false)],
    )]);
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddCheck {
            table: "account".to_string(),
            name: "ck_account_role".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };

    let pg = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(pg.statements, vec![PG_DB_CHECK_ACCOUNT_ROLE]);
}

#[test]
fn emit_sql_with_ir_orders_add_column_before_the_check_that_references_it() {
    // The single-deploy shape: a new column plus a table check over it.
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![
        transfer_outflow_table_check(),
    ])]);
    let old_ir = envelope(vec![schema_model(
        "transfer",
        vec![
            pk_col("id", "int"),
            col("inflow_activity_id", "int", true),
            col("inflow_transaction_id", "int", true),
            col("outflow_activity_id", "int", true),
        ],
    )]);
    let plan = MigrationPlan {
        operations: vec![
            MigrationOp::AddColumn {
                table: "transfer".to_string(),
                column: "outflow_transaction_id".to_string(),
            },
            MigrationOp::AddCheck {
                table: "transfer".to_string(),
                name: "ck_transfer_at_most_one_outflow".to_string(),
            },
        ],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };

    let pg = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(pg.statements.len(), 2, "{:?}", pg.statements);
    assert!(pg.statements[0].contains("ADD COLUMN \"outflow_transaction_id\""));
    assert!(pg.statements[1].contains("ADD CONSTRAINT \"ck_transfer_at_most_one_outflow\""));
}

#[test]
fn emit_sql_with_ir_add_check_fails_loudly_for_an_undeclared_name() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddCheck {
            table: "transfer".to_string(),
            name: "ck_transfer_nope".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let err = render_flat(&plan, &live_transfer_ir(), &new_ir, Dialect::Postgres).unwrap_err();
    assert!(err.message.contains("ck_transfer_nope"), "{}", err.message);
}

// ---------------------------------------------------------------------------
// Check-body rebuild (#344; ADR-0015): same live name, different body.
// ---------------------------------------------------------------------------

#[test]
fn plan_check_rebuilds_plans_a_declared_name_whose_body_drifted() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![
        transfer_outflow_table_check(),
    ])]);
    let live = [(
        "ck_transfer_at_most_one_outflow".to_string(),
        "CHECK ((\"outflow_transaction_id\" IS NULL) AND (\"outflow_activity_id\" IS NULL))"
            .to_string(),
    )];
    assert_eq!(
        plan_check_rebuilds("transfer", &new_ir, &live),
        vec![MigrationOp::RebuildCheck {
            table: "transfer".to_string(),
            name: "ck_transfer_at_most_one_outflow".to_string(),
        }]
    );
}

#[test]
fn plan_check_rebuilds_is_a_noop_when_catalog_wrapping_is_the_only_difference() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![
        transfer_outflow_table_check(),
    ])]);
    let live = [(
        "ck_transfer_at_most_one_outflow".to_string(),
        "CHECK (((outflow_transaction_id IS NULL) OR (outflow_activity_id IS NULL)))".to_string(),
    )];
    assert!(
        plan_check_rebuilds("transfer", &new_ir, &live).is_empty(),
        "catalog parens are not drift"
    );
}

#[test]
fn emit_sql_with_ir_rebuild_check_drops_then_bare_adds_on_postgres() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![
        transfer_outflow_table_check(),
    ])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::RebuildCheck {
            table: "transfer".to_string(),
            name: "ck_transfer_at_most_one_outflow".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };

    let pg = render_flat(&plan, &live_transfer_ir(), &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(
        pg.statements,
        vec![
            "ALTER TABLE \"transfer\" DROP CONSTRAINT \"ck_transfer_at_most_one_outflow\""
                .to_string(),
            "ALTER TABLE \"transfer\" ADD CONSTRAINT \"ck_transfer_at_most_one_outflow\" \
             CHECK ((\"outflow_transaction_id\" IS NULL) OR (\"outflow_activity_id\" IS NULL))"
                .to_string(),
        ]
    );
    assert!(pg.warnings.is_empty(), "{:?}", pg.warnings);

    let lite = render_flat(&plan, &live_transfer_ir(), &new_ir, Dialect::Sqlite).unwrap();
    assert!(lite.statements.is_empty(), "{:?}", lite.statements);
    assert_eq!(lite.warnings.len(), 1);
    assert!(
        lite.warnings[0].contains("ck_transfer_at_most_one_outflow"),
        "{}",
        lite.warnings[0]
    );
}

#[test]
fn emit_sql_with_ir_rebuild_check_fails_loudly_for_an_undeclared_name() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::RebuildCheck {
            table: "transfer".to_string(),
            name: "ck_transfer_nope".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let err = render_flat(&plan, &live_transfer_ir(), &new_ir, Dialect::Postgres).unwrap_err();
    assert!(err.message.contains("ck_transfer_nope"), "{}", err.message);
}

// ---------------------------------------------------------------------------
// Leftover ferro-owned CHECKs (#345; ADR-0013): live name gone from the model.
// Planned after rebuilds. User-owned names never enter live_ferro_owned_names.
// ---------------------------------------------------------------------------

#[test]
fn plan_check_drops_plans_live_ferro_owned_names_the_model_does_not_declare() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![
        transfer_outflow_table_check(),
    ])]);
    let live = vec![
        "ck_transfer_orphan".to_string(),
        "ck_transfer_at_most_one_outflow".to_string(),
        "ck_transfer_old".to_string(),
    ];
    assert_eq!(
        plan_check_drops("transfer", &new_ir, &live),
        vec![
            MigrationOp::DropCheck {
                table: "transfer".to_string(),
                name: "ck_transfer_orphan".to_string(),
            },
            MigrationOp::DropCheck {
                table: "transfer".to_string(),
                name: "ck_transfer_old".to_string(),
            },
        ]
    );
}

#[test]
fn plan_check_drops_is_a_noop_when_every_live_name_is_declared() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![
        transfer_outflow_table_check(),
    ])]);
    let live = vec!["ck_transfer_at_most_one_outflow".to_string()];
    assert!(plan_check_drops("transfer", &new_ir, &live).is_empty());
}

#[test]
fn emit_sql_with_ir_drop_check_drops_on_postgres_and_warns_on_sqlite() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::DropCheck {
            table: "transfer".to_string(),
            name: "ck_transfer_orphan".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };

    let pg = render_flat(&plan, &live_transfer_ir(), &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(
        pg.statements,
        vec![r#"ALTER TABLE "transfer" DROP CONSTRAINT "ck_transfer_orphan""#.to_string()]
    );
    assert!(pg.warnings.is_empty(), "{:?}", pg.warnings);

    let lite = render_flat(&plan, &live_transfer_ir(), &new_ir, Dialect::Sqlite).unwrap();
    assert!(lite.statements.is_empty(), "{:?}", lite.statements);
    assert_eq!(lite.warnings.len(), 1);
    assert!(
        lite.warnings[0].contains("ck_transfer_orphan"),
        "{}",
        lite.warnings[0]
    );
    assert!(
        lite.warnings[0].contains("ferro migrate new"),
        "{}",
        lite.warnings[0]
    );
    assert!(
        !lite.warnings[0].contains("Alembic"),
        "{}",
        lite.warnings[0]
    );
}

#[test]
fn emit_sql_with_ir_drop_check_fails_loudly_for_a_still_declared_name() {
    let new_ir = envelope(vec![transfer_model_with_table_checks(vec![
        transfer_outflow_table_check(),
    ])]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::DropCheck {
            table: "transfer".to_string(),
            name: "ck_transfer_at_most_one_outflow".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let err = render_flat(&plan, &live_transfer_ir(), &new_ir, Dialect::Postgres).unwrap_err();
    assert!(
        err.message.contains("ck_transfer_at_most_one_outflow"),
        "{}",
        err.message
    );
}

#[test]
fn render_check_body_quotes_column_and_joins_values() {
    let check = SchemaCheck {
        name: "ck_account_role".to_string(),
        column: "role".to_string(),
        values: vec!["'admin'".to_string(), "'user'".to_string()],
    };
    assert_eq!(
        ferro_ddl_lowering::render_check_body(&check),
        "\"role\" IN ('admin', 'user')"
    );
}

#[test]
fn render_db_check_postgres_emits_quoted_alter_no_warning() {
    let check = SchemaCheck {
        name: "ck_account_role".to_string(),
        column: "role".to_string(),
        values: vec!["'admin'".to_string(), "'user'".to_string()],
    };
    let e = ferro_ddl_lowering::render_db_check(
        "account",
        &check,
        Dialect::Postgres,
        ferro_ddl_lowering::ConstraintMode::Plain,
    );
    assert_eq!(e.statement.as_deref(), Some(PG_DB_CHECK_ACCOUNT_ROLE));
    assert!(e.warning.is_none());
}

#[test]
fn render_db_check_sqlite_renders_inline_without_warning() {
    let check = SchemaCheck {
        name: "ck_account_role".to_string(),
        column: "role".to_string(),
        values: vec!["'admin'".to_string(), "'user'".to_string()],
    };
    let e = ferro_ddl_lowering::render_db_check(
        "account",
        &check,
        Dialect::Sqlite,
        ferro_ddl_lowering::ConstraintMode::Plain,
    );
    assert!(e.statement.is_none());
    assert!(e.warning.is_none());
    assert_eq!(
        e.inline.as_deref(),
        Some("CONSTRAINT \"ck_account_role\" CHECK (\"role\" IN ('admin', 'user'))")
    );
}

#[test]
fn emit_sql_with_ir_add_column_db_check_postgres_quoted_and_sqlite_inline() {
    // A model whose `role` column has a db_check enum constraint.
    // The check name must equal test_db_check_constraint_name("account", "role")
    // so the emit_add_column matching loop fires.
    let mut model = schema_model(
        "account",
        vec![ir_col("role", "string", None, true, false, false)],
    );
    model.checks = vec![SchemaCheck {
        name: test_db_check_constraint_name("account", "role"),
        column: "role".to_string(),
        values: vec!["'admin'".to_string(), "'user'".to_string()],
    }];

    let old_ir = envelope(vec![schema_model("account", vec![])]);
    let new_ir = envelope(vec![model]);

    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddColumn {
            table: "account".to_string(),
            column: "role".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };

    let pg = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert!(
        pg.statements.iter().any(|s| s == PG_DB_CHECK_ACCOUNT_ROLE),
        "Postgres ALTER path must emit idempotent, quoted CHECK; got: {:?}",
        pg.statements
    );

    // SQLite's ADD COLUMN accepts a column CHECK: the constraint rides the
    // added column's definition, named, and nothing is elided (#514).
    let lite = render_flat(&plan, &old_ir, &new_ir, Dialect::Sqlite).unwrap();
    assert_eq!(
        lite.statements,
        vec![
            "ALTER TABLE \"account\" ADD COLUMN \"role\" varchar \
             CONSTRAINT \"ck_account_role\" CHECK (\"role\" IN ('admin', 'user'))"
                .to_string()
        ]
    );
    assert!(
        lite.warnings.is_empty(),
        "unexpected warnings: {:?}",
        lite.warnings
    );
}

#[test]
fn order_models_for_create_self_fk_is_not_an_ordering_constraint() {
    // #302: `znode` carries a self-referential FK; `areferrer` arrives first
    // (mirroring the alphabetical incoming order) and requires `znode`. The
    // self-loop is satisfied by the table's own CREATE, so it must not evict
    // the component from the dependency order.
    let znode = SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            renamed_from: None,
            column: "parent_id".to_string(),
            to_table: "znode".to_string(),
            to_column: "id".to_string(),
            on_delete: None,
            name: None,
        }],
        ..schema_model("znode", vec![col("id", "uuid", false)])
    };
    let areferrer = SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            renamed_from: None,
            column: "node_id".to_string(),
            to_table: "znode".to_string(),
            to_column: "id".to_string(),
            on_delete: None,
            name: None,
        }],
        ..schema_model("areferrer", vec![col("id", "uuid", false)])
    };

    let models = vec![&areferrer, &znode];
    let ordered = order_models_for_create(&models);
    let tables: Vec<&str> = ordered.iter().map(|m| m.table_name.as_str()).collect();
    assert_eq!(tables, vec!["znode", "areferrer"]);
}

// ---------------------------------------------------------------------------
// Foreign-key reconciliation (#325)
// ---------------------------------------------------------------------------

fn fk(
    column: &str,
    to_table: &str,
    on_delete: Option<&str>,
    name: Option<&str>,
) -> SchemaForeignKey {
    SchemaForeignKey {
        renamed_from: None,
        column: column.to_string(),
        to_table: to_table.to_string(),
        to_column: "id".to_string(),
        on_delete: on_delete.map(str::to_string),
        name: name.map(str::to_string),
    }
}

fn schema_model_with_fks(
    table: &str,
    cols: Vec<SchemaColumn>,
    fks: Vec<SchemaForeignKey>,
) -> SchemaModel {
    let mut model = schema_model(table, cols);
    model.foreign_keys = fks;
    model
}

#[test]
fn plan_from_ir_rebuilds_fk_on_delete_drift() {
    let old_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk(
            "connection_id",
            "connection",
            Some("CASCADE"),
            Some("fk_account_connection_id_connection"),
        )],
    )]);
    let new_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk(
            "connection_id",
            "connection",
            Some("SET NULL"),
            Some("fk_account_connection_id_connection"),
        )],
    )]);

    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Postgres,
        &LiveFacts::declared(),
        destructive(),
    );
    assert_eq!(
        plan.operations,
        vec![MigrationOp::RebuildForeignKey {
            table: "account".to_string(),
            column: "connection_id".to_string(),
            old_name: "fk_account_connection_id_connection".to_string(),
        }]
    );
    assert!(plan.warnings.is_empty());
}

#[test]
fn plan_from_ir_fk_noop_when_definition_matches() {
    // Declared None means CASCADE — a live CASCADE constraint is not drift.
    let old_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk(
            "connection_id",
            "connection",
            Some("CASCADE"),
            Some("fk_account_connection_id_connection"),
        )],
    )]);
    let new_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk(
            "connection_id",
            "connection",
            None,
            Some("fk_account_connection_id_connection"),
        )],
    )]);

    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Postgres,
        &LiveFacts::declared(),
        destructive(),
    );
    assert!(
        plan.operations.is_empty(),
        "unexpected ops: {:?}",
        plan.operations
    );
    assert!(plan.warnings.is_empty());
}

#[test]
fn plan_from_ir_adds_fk_missing_on_existing_column() {
    let old_ir = envelope(vec![schema_model(
        "account",
        vec![col("connection_id", "integer", true)],
    )]);
    let new_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk(
            "connection_id",
            "connection",
            Some("SET NULL"),
            Some("fk_account_connection_id_connection"),
        )],
    )]);

    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Postgres,
        &LiveFacts::declared(),
        destructive(),
    );
    assert_eq!(
        plan.operations,
        vec![MigrationOp::AddForeignKey {
            table: "account".to_string(),
            column: "connection_id".to_string(),
        }]
    );
}

#[test]
fn plan_from_ir_fk_on_new_column_rides_add_column() {
    // The FK's column does not exist live: AddColumn emission owns the
    // constraint, so no standalone FK op may be planned.
    let old_ir = envelope(vec![schema_model(
        "account",
        vec![col("id", "integer", false)],
    )]);
    let new_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![
            col("id", "integer", false),
            col("connection_id", "integer", true),
        ],
        vec![fk(
            "connection_id",
            "connection",
            Some("SET NULL"),
            Some("fk_account_connection_id_connection"),
        )],
    )]);

    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Postgres,
        &LiveFacts::declared(),
        destructive(),
    );
    assert_eq!(
        plan.operations,
        vec![MigrationOp::AddColumn {
            table: "account".to_string(),
            column: "connection_id".to_string(),
        }]
    );
}

#[test]
fn plan_from_ir_warns_on_user_owned_fk_drift() {
    let old_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk(
            "connection_id",
            "connection",
            Some("CASCADE"),
            Some("account_connection_id_fkey"),
        )],
    )]);
    let new_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk(
            "connection_id",
            "connection",
            Some("SET NULL"),
            Some("fk_account_connection_id_connection"),
        )],
    )]);

    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Postgres,
        &LiveFacts::declared(),
        destructive(),
    );
    assert!(
        plan.operations.is_empty(),
        "unexpected ops: {:?}",
        plan.operations
    );
    assert_eq!(plan.warnings.len(), 1);
    assert!(
        plan.warnings[0].contains("not ferro-owned"),
        "{}",
        plan.warnings[0]
    );
    assert!(plan.warnings[0].contains("account_connection_id_fkey"));
}

#[test]
fn plan_from_ir_unnamed_live_fk_drift_rebuilds_with_canonical_name() {
    // SQLite live FKs carry no constraint name; the op falls back to the
    // canonical name (emission warns on SQLite anyway).
    let old_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk("connection_id", "connection", Some("CASCADE"), None)],
    )]);
    let new_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk(
            "connection_id",
            "connection",
            Some("SET NULL"),
            Some("fk_account_connection_id_connection"),
        )],
    )]);

    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
    assert_eq!(
        plan.operations,
        vec![MigrationOp::RebuildForeignKey {
            table: "account".to_string(),
            column: "connection_id".to_string(),
            old_name: "fk_account_connection_id_connection".to_string(),
        }]
    );
}

#[test]
fn emit_sql_with_ir_rebuild_fk_postgres_drops_then_adds() {
    let new_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk(
            "connection_id",
            "connection",
            Some("SET NULL"),
            Some("fk_account_connection_id_connection"),
        )],
    )]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::RebuildForeignKey {
            table: "account".to_string(),
            column: "connection_id".to_string(),
            old_name: "fk_account_connection_id_connection".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };

    let result = render_flat(&plan, &empty_envelope(), &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(
        result.statements,
        vec![
            "ALTER TABLE \"account\" DROP CONSTRAINT \"fk_account_connection_id_connection\""
                .to_string(),
            "ALTER TABLE \"account\" ADD CONSTRAINT \"fk_account_connection_id_connection\" \
             FOREIGN KEY (\"connection_id\") REFERENCES \"connection\" (\"id\") \
             ON DELETE SET NULL"
                .to_string(),
        ]
    );
    assert!(result.warnings.is_empty());
}

#[test]
fn emit_sql_with_ir_rebuild_fk_sqlite_warns_and_skips() {
    let old_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk("connection_id", "connection", Some("CASCADE"), None)],
    )]);
    let new_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk(
            "connection_id",
            "connection",
            Some("SET NULL"),
            Some("fk_account_connection_id_connection"),
        )],
    )]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::RebuildForeignKey {
            table: "account".to_string(),
            column: "connection_id".to_string(),
            old_name: "fk_account_connection_id_connection".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };

    let result = render_flat(&plan, &old_ir, &new_ir, Dialect::Sqlite).unwrap();
    assert!(
        result.statements.is_empty(),
        "unexpected DDL: {:?}",
        result.statements
    );
    assert_eq!(result.warnings.len(), 1);
    assert!(
        result.warnings[0].contains("on_delete SET NULL"),
        "{}",
        result.warnings[0]
    );
    assert!(
        result.warnings[0].contains("CASCADE"),
        "{}",
        result.warnings[0]
    );
}

#[test]
fn emit_sql_with_ir_add_fk_postgres_and_sqlite() {
    let new_ir = envelope(vec![schema_model_with_fks(
        "account",
        vec![col("connection_id", "integer", true)],
        vec![fk(
            "connection_id",
            "connection",
            Some("SET NULL"),
            Some("fk_account_connection_id_connection"),
        )],
    )]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddForeignKey {
            table: "account".to_string(),
            column: "connection_id".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };

    let pg = render_flat(&plan, &empty_envelope(), &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(
        pg.statements,
        vec![
            "ALTER TABLE \"account\" ADD CONSTRAINT \"fk_account_connection_id_connection\" \
             FOREIGN KEY (\"connection_id\") REFERENCES \"connection\" (\"id\") \
             ON DELETE SET NULL"
                .to_string(),
        ]
    );

    let sqlite = render_flat(&plan, &empty_envelope(), &new_ir, Dialect::Sqlite).unwrap();
    assert!(sqlite.statements.is_empty());
    assert_eq!(sqlite.warnings.len(), 1);
    assert!(sqlite.warnings[0].contains("SQLite cannot add table constraints"));
}

// ---------------------------------------------------------------------------
// Row security in the create pass (#409). A NEW table gets its flags and
// policies WITH its creation; ADR-0010 keeps this pass off existing tables, so
// there is nothing here for a table that already exists (that is #413).
// ---------------------------------------------------------------------------

fn ledgerrow_model_with_row_security(force: bool) -> SchemaModel {
    SchemaModel {
        row_security: Some(ferro_schema_ir::SchemaRowSecurity {
            force,
            policies: vec![ferro_schema_ir::SchemaRowPolicy {
                name: "rls_ledgerrow_ledger_id".to_string(),
                command: ferro_schema_ir::RowPolicyCommand::All,
                restrictive: false,
                expr: ferro_schema_ir::RowPolicyExpr::Setting {
                    column: "ledger_id".to_string(),
                    setting: "pinch.ledger_id".to_string(),
                },
            }],
        }),
        ..schema_model(
            "ledgerrow",
            vec![pk_col("id", "int"), col("ledger_id", "uuid", false)],
        )
    }
}

#[test]
fn create_pass_emits_row_security_after_the_tables_other_artifacts() {
    let model = ledgerrow_model_with_row_security(true);
    let emission = render_create_table(&model, Dialect::Postgres).unwrap();
    assert!(emission.create_sql.starts_with("CREATE TABLE"));
    assert_eq!(
        emission.post_create_sqls,
        vec![
            "ALTER TABLE \"ledgerrow\" ENABLE ROW LEVEL SECURITY".to_string(),
            "ALTER TABLE \"ledgerrow\" FORCE ROW LEVEL SECURITY".to_string(),
            "CREATE POLICY \"rls_ledgerrow_ledger_id\" ON \"ledgerrow\" FOR ALL \
             USING (\"ledger_id\" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid) \
             WITH CHECK (\"ledger_id\" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid)"
                .to_string(),
        ]
    );
    assert!(emission.warnings.is_empty());
}

#[test]
fn create_pass_omits_force_when_the_declaration_does() {
    let model = ledgerrow_model_with_row_security(false);
    let emission = render_create_table(&model, Dialect::Postgres).unwrap();
    assert!(
        !emission
            .post_create_sqls
            .iter()
            .any(|sql| sql.contains("FORCE")),
        "{:?}",
        emission.post_create_sqls
    );
}

#[test]
fn create_pass_skips_row_security_on_sqlite_with_one_warning() {
    let model = ledgerrow_model_with_row_security(true);
    let emission = render_create_table(&model, Dialect::Sqlite).unwrap();
    assert!(emission.create_sql.starts_with("CREATE TABLE"));
    assert!(
        !emission
            .post_create_sqls
            .iter()
            .any(|sql| sql.contains("ROW LEVEL SECURITY") || sql.contains("CREATE POLICY")),
        "{:?}",
        emission.post_create_sqls
    );
    assert_eq!(emission.warnings.len(), 1);
    assert!(emission.warnings[0].contains("ledgerrow"));
    assert!(emission.warnings[0].contains("PostgreSQL-only"));
}

#[test]
fn add_table_pass_carries_row_security_through_emit_sql_with_ir() {
    let model = ledgerrow_model_with_row_security(true);
    let new_ir = envelope(vec![model]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AddTable {
            table: "ledgerrow".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let emitted = render_flat(&plan, &empty_envelope(), &new_ir, Dialect::Postgres).unwrap();
    assert!(
        emitted
            .statements
            .iter()
            .any(|sql| sql == "ALTER TABLE \"ledgerrow\" ENABLE ROW LEVEL SECURITY"),
        "{:?}",
        emitted.statements
    );
    assert!(
        emitted
            .statements
            .iter()
            .any(|sql| sql.starts_with("CREATE POLICY \"rls_ledgerrow_ledger_id\"")),
        "{:?}",
        emitted.statements
    );
}

// ---------------------------------------------------------------------------
// Validity flags (#515; ADR-0043, ADR-0044): a live constraint or index can
// exist and still not be trusted. A declared FK or check that exists live
// `NOT VALID` is validated in place; a declared index that exists live
// invalid is rebuilt (drop, then the create statement the add path renders).
// ---------------------------------------------------------------------------

/// `post` with a ferro-named FK to `author`, a table check, a composite
/// index and a unique — every artifact kind a validity flag attaches to.
fn post_model_with_constraints() -> SchemaModel {
    SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            renamed_from: None,
            column: "author_id".to_string(),
            to_table: "author".to_string(),
            to_column: "id".to_string(),
            on_delete: Some("CASCADE".to_string()),
            name: Some("fk_post_author_id_author".to_string()),
        }],
        indexes: vec![SchemaIndex {
            name: "idx_post_author_id_title".to_string(),
            columns: vec!["author_id".to_string(), "title".to_string()],
            unique: false,
        }],
        uniques: vec![SchemaUnique {
            name: "uq_post_slug".to_string(),
            columns: vec!["slug".to_string()],
        }],
        table_checks: vec![SchemaTableCheck {
            name: "ck_post_title_set".to_string(),
            predicate: CheckExpr::IsNotNull {
                column: "title".to_string(),
            },
        }],
        ..schema_model(
            "post",
            vec![
                pk_col("id", "int"),
                col("author_id", "int", true),
                col("slug", "text", true),
                col("title", "text", true),
            ],
        )
    }
}

fn fk_validity(name: &str, validated: bool) -> LiveFkValidity {
    LiveFkValidity {
        name: name.to_string(),
        validated,
    }
}

fn check_validity(name: &str, validated: bool) -> LiveCheckValidity {
    LiveCheckValidity {
        name: name.to_string(),
        validated,
    }
}

fn index_validity(name: &str, valid: bool) -> LiveIndexValidity {
    LiveIndexValidity {
        name: name.to_string(),
        valid,
    }
}

#[test]
fn plan_validations_validates_a_declared_fk_and_check_that_exist_not_valid() {
    let new_ir = envelope(vec![post_model_with_constraints()]);
    assert_eq!(
        plan_validations(
            "post",
            &new_ir,
            &[fk_validity("fk_post_author_id_author", false)],
            &[check_validity("ck_post_title_set", false)],
        ),
        vec![
            MigrationOp::ValidateConstraint {
                table: "post".to_string(),
                name: "fk_post_author_id_author".to_string(),
            },
            MigrationOp::ValidateConstraint {
                table: "post".to_string(),
                name: "ck_post_title_set".to_string(),
            },
        ]
    );
}

#[test]
fn plan_validations_is_a_noop_for_validated_absent_or_undeclared_constraints() {
    let new_ir = envelope(vec![post_model_with_constraints()]);
    assert!(
        plan_validations(
            "post",
            &new_ir,
            &[fk_validity("fk_post_author_id_author", true)],
            &[check_validity("ck_post_title_set", true)],
        )
        .is_empty(),
        "a validated constraint replans to nothing"
    );
    assert!(
        plan_validations("post", &new_ir, &[], &[]).is_empty(),
        "a constraint absent live is an add, never a validate"
    );
    assert!(
        plan_validations(
            "post",
            &new_ir,
            &[fk_validity("post_author_id_fkey", false)],
            &[
                check_validity("ck_post_orphan", false),
                check_validity("user_check", false)
            ],
        )
        .is_empty(),
        "a NOT VALID constraint the model does not declare is never touched"
    );
    assert!(
        plan_validations("missing", &new_ir, &[], &[]).is_empty(),
        "an undeclared table plans nothing"
    );
}

#[test]
fn plan_index_rebuilds_rebuilds_a_declared_index_that_exists_invalid() {
    let new_ir = envelope(vec![post_model_with_constraints()]);
    assert_eq!(
        plan_index_rebuilds(
            "post",
            &new_ir,
            &[
                index_validity("idx_post_author_id_title", false),
                index_validity("uq_post_slug", false),
            ],
        ),
        vec![
            MigrationOp::RebuildIndex {
                table: "post".to_string(),
                name: "idx_post_author_id_title".to_string(),
                columns: vec!["author_id".to_string(), "title".to_string()],
                unique: false,
            },
            MigrationOp::RebuildIndex {
                table: "post".to_string(),
                name: "uq_post_slug".to_string(),
                columns: vec!["slug".to_string()],
                unique: true,
            },
        ]
    );
}

#[test]
fn plan_index_rebuilds_is_a_noop_for_valid_absent_or_undeclared_indexes() {
    let new_ir = envelope(vec![post_model_with_constraints()]);
    assert!(
        plan_index_rebuilds(
            "post",
            &new_ir,
            &[
                index_validity("idx_post_author_id_title", true),
                index_validity("uq_post_slug", true),
            ],
        )
        .is_empty()
    );
    assert!(plan_index_rebuilds("post", &new_ir, &[]).is_empty());
    assert!(
        plan_index_rebuilds("post", &new_ir, &[index_validity("idx_post_legacy", false)])
            .is_empty(),
        "an invalid index the model does not declare is leftover handling, not a rebuild"
    );
}

#[test]
fn emit_validate_constraint_renders_the_one_validate_statement_on_postgres() {
    let new_ir = envelope(vec![post_model_with_constraints()]);
    let plan = MigrationPlan {
        operations: vec![
            MigrationOp::ValidateConstraint {
                table: "post".to_string(),
                name: "fk_post_author_id_author".to_string(),
            },
            MigrationOp::ValidateConstraint {
                table: "post".to_string(),
                name: "ck_post_title_set".to_string(),
            },
        ],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let pg = render_flat(&plan, &new_ir, &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(
        pg.statements,
        vec![
            ferro_ddl_lowering::render_validate_constraint("post", "fk_post_author_id_author"),
            ferro_ddl_lowering::render_validate_constraint("post", "ck_post_title_set"),
        ]
    );
    assert_eq!(
        pg.statements[0],
        "ALTER TABLE \"post\" VALIDATE CONSTRAINT \"fk_post_author_id_author\""
    );
    assert!(pg.warnings.is_empty(), "{:?}", pg.warnings);
}

#[test]
fn emit_validate_constraint_fails_loudly_on_sqlite() {
    // SQLite has no unvalidated constraints (its flags always read `true`),
    // so the planner never plans the op there; an op that reaches emission
    // anyway is a planner bug, never a silent no-op.
    let new_ir = envelope(vec![post_model_with_constraints()]);
    let plan = MigrationPlan {
        operations: vec![MigrationOp::ValidateConstraint {
            table: "post".to_string(),
            name: "ck_post_title_set".to_string(),
        }],
        warnings: Vec::new(),
        always_warnings: Vec::new(),
    };
    let err = render_flat(&plan, &new_ir, &new_ir, Dialect::Sqlite).unwrap_err();
    assert!(err.message.contains("ck_post_title_set"), "{}", err.message);
}

#[test]
fn emit_rebuild_index_drops_then_runs_the_add_index_create_statement() {
    let add = MigrationPlan {
        operations: vec![MigrationOp::AddIndex {
            table: "post".into(),
            name: "uq_post_slug".into(),
            columns: vec!["slug".into()],
            unique: true,
        }],
        warnings: vec![],
        always_warnings: Vec::new(),
    };
    let rebuild = MigrationPlan {
        operations: vec![MigrationOp::RebuildIndex {
            table: "post".into(),
            name: "uq_post_slug".into(),
            columns: vec!["slug".into()],
            unique: true,
        }],
        warnings: vec![],
        always_warnings: Vec::new(),
    };
    let created = render_flat(
        &add,
        &empty_envelope(),
        &empty_envelope(),
        Dialect::Postgres,
    )
    .unwrap()
    .statements;
    let rebuilt = render_flat(
        &rebuild,
        &empty_envelope(),
        &empty_envelope(),
        Dialect::Postgres,
    )
    .unwrap();
    assert_eq!(
        rebuilt.statements,
        vec![
            "DROP INDEX \"uq_post_slug\"".to_string(),
            "CREATE UNIQUE INDEX IF NOT EXISTS \"uq_post_slug\" ON \"post\" (\"slug\")".to_string(),
        ]
    );
    assert_eq!(
        rebuilt.statements[1..],
        created[..],
        "the rebuild's create is byte-identical to the add path's"
    );
    assert!(rebuilt.warnings.is_empty(), "{:?}", rebuilt.warnings);
}

#[test]
fn a_not_valid_check_whose_body_drifted_is_a_rebuild_and_an_unchanged_one_is_not() {
    // The rebuild's bare ADD installs a valid constraint; the caller drops
    // the validate for any name a rebuild already covers. Pin the rebuild
    // planner's verdict for both NOT VALID shapes.
    let new_ir = envelope(vec![post_model_with_constraints()]);
    let drifted = [(
        "ck_post_title_set".to_string(),
        "CHECK ((title IS NULL)) NOT VALID".to_string(),
    )];
    assert_eq!(
        plan_check_rebuilds("post", &new_ir, &drifted),
        vec![MigrationOp::RebuildCheck {
            table: "post".to_string(),
            name: "ck_post_title_set".to_string(),
        }]
    );
    let unchanged = [(
        "ck_post_title_set".to_string(),
        "CHECK ((title IS NOT NULL)) NOT VALID".to_string(),
    )];
    assert!(
        plan_check_rebuilds("post", &new_ir, &unchanged).is_empty(),
        "a NOT VALID check with the declared body is a validate, never a rebuild"
    );
}

#[test]
fn render_plan_renders_the_validate_and_rebuild_index_ops() {
    let new_ir = envelope(vec![post_model_with_constraints()]);
    let plan = MigrationPlan {
        operations: vec![
            MigrationOp::ValidateConstraint {
                table: "post".into(),
                name: "ck_post_title_set".into(),
            },
            MigrationOp::RebuildIndex {
                table: "post".into(),
                name: "uq_post_slug".into(),
                columns: vec!["slug".into()],
                unique: true,
            },
        ],
        warnings: vec![],
        always_warnings: Vec::new(),
    };
    let rendered = render_plan(&plan, &new_ir, &new_ir, Dialect::Postgres).unwrap();
    assert_eq!(
        rendered[0].statements,
        vec!["ALTER TABLE \"post\" VALIDATE CONSTRAINT \"ck_post_title_set\"".to_string()]
    );
    assert_eq!(rendered[1].statements[0], "DROP INDEX \"uq_post_slug\"");
}

// ---------------------------------------------------------------------------
// The one planner (#517; ADR-0027, ADR-0041): `plan_from_ir` decides the whole
// modelset — enum labels and type creation first, then tables in dependency
// order, each table's column/index/FK/check/row-security ops in the order the
// reconciliation pass executes them — and `render_plan` renders each op.
// ---------------------------------------------------------------------------

fn destructive() -> PlanOptions {
    PlanOptions { destructive: true }
}

fn updates_only() -> PlanOptions {
    PlanOptions { destructive: false }
}

fn status_enum_col(name: &str, labels: &[&str], nullable: bool) -> SchemaColumn {
    SchemaColumn {
        enum_values: Some(labels.iter().map(|l| serde_json::json!(l)).collect()),
        enum_type_name: Some("status".to_string()),
        db_type: None,
        ..col(name, "text", nullable)
    }
}

/// `parent(id)` and `child(id, parent_id → parent, status status)`.
fn parent_child_models(labels: &[&str]) -> Vec<SchemaModel> {
    let parent = schema_model("parent", vec![pk_col("id", "int")]);
    let child = SchemaModel {
        foreign_keys: vec![fk("parent_id", "parent", Some("CASCADE"), None)],
        ..schema_model(
            "child",
            vec![
                pk_col("id", "int"),
                col("parent_id", "int", true),
                status_enum_col("status", labels, true),
            ],
        )
    };
    // Child first in the envelope: the planner, not the input order, decides.
    vec![child, parent]
}

#[test]
fn whole_modelset_add_creates_types_first_then_parents_before_children() {
    let new = envelope(parent_child_models(&["draft", "archived"]));
    let plan = plan_from_ir(
        &empty_envelope(),
        &new,
        Dialect::Postgres,
        &LiveFacts::declared(),
        destructive(),
    );
    assert_eq!(
        plan.operations,
        vec![
            MigrationOp::CreateEnumType {
                type_name: "status".into(),
                labels: vec!["draft".into(), "archived".into()],
            },
            MigrationOp::AddTable {
                table: "parent".into()
            },
            MigrationOp::AddTable {
                table: "child".into()
            },
        ]
    );
    let rendered = render_plan(&plan, &empty_envelope(), &new, Dialect::Postgres).unwrap();
    let statements: Vec<&String> = rendered.iter().flat_map(|op| &op.statements).collect();
    assert_eq!(
        statements
            .iter()
            .filter(|s| s.contains("CREATE TYPE"))
            .count(),
        1,
        "the type is created once, by its own op: {statements:?}"
    );
    assert!(statements[0].contains("CREATE TYPE \"status\""));
    assert!(statements[1].starts_with("CREATE TABLE IF NOT EXISTS \"parent\""));

    let sqlite = plan_from_ir(
        &empty_envelope(),
        &new,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
    assert_eq!(
        sqlite.operations,
        vec![
            MigrationOp::AddTable {
                table: "parent".into()
            },
            MigrationOp::AddTable {
                table: "child".into()
            },
        ],
        "SQLite has no native enum types"
    );
}

#[test]
fn whole_modelset_drop_removes_children_before_parents_then_their_types() {
    let old = envelope(parent_child_models(&["draft", "archived"]));
    let plan = plan_from_ir(
        &old,
        &empty_envelope(),
        Dialect::Postgres,
        &LiveFacts::declared(),
        destructive(),
    );
    assert_eq!(
        plan.operations,
        vec![
            MigrationOp::DropTable {
                table: "child".into()
            },
            MigrationOp::DropTable {
                table: "parent".into()
            },
            MigrationOp::DropEnumType {
                type_name: "status".into()
            },
        ]
    );
    let kept = plan_from_ir(
        &old,
        &empty_envelope(),
        Dialect::Postgres,
        &LiveFacts::declared(),
        updates_only(),
    );
    assert!(kept.operations.is_empty(), "{:?}", kept.operations);
}

#[test]
fn label_addition_precedes_every_table_op_including_the_tables_using_the_type() {
    let old = envelope(parent_child_models(&["draft"]));
    let mut models = parent_child_models(&["draft", "archived"]);
    models[0].columns.push(col("note", "text", true));
    let new = envelope(models);
    let plan = plan_from_ir(
        &old,
        &new,
        Dialect::Postgres,
        &LiveFacts::declared(),
        destructive(),
    );
    assert_eq!(
        plan.operations,
        vec![
            MigrationOp::AddEnumLabel {
                type_name: "status".into(),
                label: "archived".into(),
            },
            MigrationOp::AddColumn {
                table: "child".into(),
                column: "note".into(),
            },
        ]
    );
    let rendered = render_plan(&plan, &old, &new, Dialect::Postgres).unwrap();
    assert_eq!(
        rendered[0].statements,
        vec!["ALTER TYPE \"status\" ADD VALUE IF NOT EXISTS 'archived'".to_string()]
    );
}

#[test]
fn live_labels_come_from_the_facts_and_extras_only_warn() {
    let models = envelope(parent_child_models(&["draft", "archived"]));
    let mut facts = LiveFacts::declared();
    facts
        .enum_labels
        .insert("status".into(), vec!["draft".into(), "legacy".into()]);
    let plan = plan_from_ir(&models, &models, Dialect::Postgres, &facts, destructive());
    assert_eq!(
        plan.operations,
        vec![MigrationOp::AddEnumLabel {
            type_name: "status".into(),
            label: "archived".into(),
        }]
    );
    assert_eq!(plan.warnings.len(), 1, "{:?}", plan.warnings);
    assert!(plan.warnings[0].contains("'legacy'"));
}

/// Two new tables, each introducing its own enum type: every type is created
/// first, by type name, then the tables in dependency order — the sequence
/// the create pass executes too (#518; AGENTS.md § I-12).
#[test]
fn new_tables_create_every_enum_type_first_by_name_then_the_tables() {
    let enum_col = |name: &str, type_name: &str, labels: &[&str]| SchemaColumn {
        enum_values: Some(labels.iter().map(|l| serde_json::json!(l)).collect()),
        enum_type_name: Some(type_name.to_string()),
        db_type: None,
        ..col(name, "text", false)
    };
    let author = schema_model(
        "author",
        vec![
            pk_col("id", "int"),
            enum_col("status", "status", &["draft", "live"]),
        ],
    );
    let post = SchemaModel {
        foreign_keys: vec![SchemaForeignKey {
            renamed_from: None,
            column: "author_id".to_string(),
            to_table: "author".to_string(),
            to_column: "id".to_string(),
            on_delete: None,
            name: None,
        }],
        ..schema_model(
            "post",
            vec![
                pk_col("id", "int"),
                col("author_id", "int", false),
                enum_col("kind", "kind", &["note", "essay"]),
            ],
        )
    };
    // Child declared first: the planner, not declaration order, decides.
    let new_ir = envelope(vec![post.clone(), author.clone()]);
    let old_ir = empty_envelope();
    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Postgres,
        &LiveFacts::declared(),
        PlanOptions::default(),
    );
    let statements = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres)
        .expect("render")
        .statements;
    let shape: Vec<String> = statements
        .iter()
        .map(|sql| {
            if sql.contains("CREATE TYPE \"kind\"") {
                "type kind".to_string()
            } else if sql.contains("CREATE TYPE \"status\"") {
                "type status".to_string()
            } else {
                sql.split('(').next().unwrap_or_default().trim().to_string()
            }
        })
        .collect();
    assert_eq!(
        shape,
        [
            "type kind",
            "type status",
            "CREATE TABLE IF NOT EXISTS \"author\"",
            "CREATE TABLE IF NOT EXISTS \"post\"",
        ],
        "{statements:#?}"
    );
}

/// A declared unique lives in `uniques`, not `indexes`; planning two
/// identical declared snapshots must read it on both sides, or every
/// `unique=True` column is a phantom `AddIndex` (#518).
#[test]
fn identical_snapshots_with_a_unique_column_plan_nothing() {
    let model = SchemaModel {
        uniques: vec![SchemaUnique {
            name: "uq_author_name".to_string(),
            columns: vec!["name".to_string()],
        }],
        indexes: vec![SchemaIndex {
            name: "idx_author_email".to_string(),
            columns: vec!["email".to_string()],
            unique: false,
        }],
        ..schema_model(
            "author",
            vec![
                pk_col("id", "int"),
                col_with_flags("name", "varchar", false, true, false, None),
                col_with_flags("email", "varchar", false, false, true, None),
            ],
        )
    };
    let snapshot = envelope(vec![model]);
    for dialect in [Dialect::Postgres, Dialect::Sqlite] {
        for destructive in [false, true] {
            let plan = plan_from_ir(
                &snapshot,
                &snapshot,
                dialect,
                &LiveFacts::declared(),
                PlanOptions { destructive },
            );
            assert!(
                plan.operations.is_empty(),
                "{dialect:?} destructive={destructive}: {:?}",
                plan.operations
            );
        }
    }
}

#[test]
fn identical_snapshots_with_checks_policies_and_types_plan_nothing() {
    let mut model = ledgerrow_model_with_row_security(true);
    model
        .columns
        .push(status_enum_col("status", &["draft"], true));
    model.table_checks.push(SchemaTableCheck {
        name: "ck_ledgerrow_ledger_set".into(),
        predicate: CheckExpr::IsNotNull {
            column: "ledger_id".into(),
        },
    });
    let both = envelope(vec![model]);
    for dialect in [Dialect::Postgres, Dialect::Sqlite] {
        let plan = plan_from_ir(&both, &both, dialect, &LiveFacts::declared(), destructive());
        assert!(plan.is_empty(), "{dialect:?}: {:?}", plan.operations);
        assert!(plan.warnings.is_empty(), "{dialect:?}: {:?}", plan.warnings);
        assert!(
            plan.always_warnings.is_empty(),
            "{dialect:?}: {:?}",
            plan.always_warnings
        );
    }
}

fn live_check(name: &str, definition: &str) -> LiveCheckFact {
    LiveCheckFact {
        name: name.into(),
        definition: definition.into(),
        ferro_owned: name.starts_with("ck_"),
        validated: true,
    }
}

#[test]
fn a_text_comparison_check_as_postgres_prints_it_is_not_a_rebuild() {
    // `Check("named", lambda t: t.name != "")` over a varchar column: ferro
    // renders `"name" <> ''`, `pg_get_constraintdef` prints the text casts
    // Postgres inserted. Same predicate, so no RebuildCheck on every connect.
    let model = SchemaModel {
        table_checks: vec![SchemaTableCheck {
            name: "ck_cknamed_named".to_string(),
            predicate: CheckExpr::Cmp {
                column: "name".to_string(),
                op: ferro_schema_ir::CheckCmpOp::Ne,
                other: ferro_schema_ir::CheckOperand::Literal {
                    token: "''".to_string(),
                },
            },
        }],
        ..schema_model(
            "cknamed",
            vec![pk_col("id", "int"), col("name", "varchar", false)],
        )
    };
    let declared = envelope(vec![model]);
    let facts_with = |definition: &str| {
        let mut facts = LiveFacts::declared();
        facts.tables.insert(
            "cknamed".into(),
            LiveTableFacts {
                checks: vec![live_check("ck_cknamed_named", definition)],
                foreign_keys: vec![],
                indexes: vec![],
                row_security: ferro_ddl_lowering::LiveRowSecurity::default(),
            },
        );
        facts
    };

    let clean = plan_from_ir(
        &declared,
        &declared,
        Dialect::Postgres,
        &facts_with("CHECK (((name)::text <> ''::text))"),
        updates_only(),
    );
    assert!(clean.operations.is_empty(), "{:?}", clean.operations);

    let drifted = plan_from_ir(
        &declared,
        &declared,
        Dialect::Postgres,
        &facts_with("CHECK (((name)::text <> 'x'::text))"),
        updates_only(),
    );
    assert_eq!(
        drifted.operations,
        vec![MigrationOp::RebuildCheck {
            table: "cknamed".into(),
            name: "ck_cknamed_named".into(),
        }],
        "a changed literal is still drift"
    );
}

fn live_policy(name: &str, using: &str) -> ferro_ddl_lowering::LiveRowPolicy {
    ferro_ddl_lowering::LiveRowPolicy {
        name: name.into(),
        command: "all".into(),
        restrictive: false,
        using: Some(using.into()),
        with_check: Some(using.into()),
        roles: vec!["public".into()],
        ferro_owned: name.starts_with("rls_"),
    }
}

/// The shorthand body as `pg_get_expr` prints it.
const CATALOG_LEDGER_EXPR: &str =
    "(ledger_id = (NULLIF(current_setting('pinch.ledger_id'::text, true), ''::text))::uuid)";

/// Live `ledgerrow`: one extra column, a leftover ferro check, a leftover
/// ferro policy, and the declared policy in place.
fn ledgerrow_with_leftovers() -> (IrEnvelope<SchemaIrPayload>, LiveFacts) {
    let mut live_model = ledgerrow_model_with_row_security(true);
    live_model.row_security = None;
    live_model.columns.push(col("legacy", "text", true));
    let mut facts = LiveFacts::declared();
    facts.tables.insert(
        "ledgerrow".into(),
        LiveTableFacts {
            checks: vec![live_check(
                "ck_ledgerrow_old",
                "CHECK ((ledger_id IS NOT NULL))",
            )],
            foreign_keys: vec![],
            indexes: vec![],
            row_security: ferro_ddl_lowering::LiveRowSecurity {
                enabled: true,
                forced: true,
                policies: vec![
                    live_policy("rls_ledgerrow_ledger_id", CATALOG_LEDGER_EXPR),
                    live_policy("rls_ledgerrow_retired", CATALOG_LEDGER_EXPR),
                ],
            },
        },
    );
    (envelope(vec![live_model]), facts)
}

#[test]
fn updates_only_plans_no_drop_and_keeps_every_leftover_warning() {
    let (live, facts) = ledgerrow_with_leftovers();
    let declared = envelope(vec![ledgerrow_model_with_row_security(true)]);

    let kept = plan_from_ir(&live, &declared, Dialect::Postgres, &facts, updates_only());
    assert!(kept.operations.is_empty(), "{:?}", kept.operations);
    assert!(
        kept.warnings
            .iter()
            .any(|w| w.contains("'ck_ledgerrow_old'")),
        "{:?}",
        kept.warnings
    );
    assert!(
        kept.always_warnings
            .iter()
            .any(|w| w.contains("'rls_ledgerrow_retired'")),
        "{:?}",
        kept.always_warnings
    );

    let dropped = plan_from_ir(&live, &declared, Dialect::Postgres, &facts, destructive());
    assert_eq!(
        dropped.operations,
        vec![
            MigrationOp::DropCheck {
                table: "ledgerrow".into(),
                name: "ck_ledgerrow_old".into(),
            },
            MigrationOp::DropRowPolicy {
                table: "ledgerrow".into(),
                name: "rls_ledgerrow_retired".into(),
            },
            MigrationOp::DropColumn {
                table: "ledgerrow".into(),
                column: "legacy".into(),
            },
        ],
        "drops: checks with the checks, policies with row security, columns last"
    );
}

#[test]
fn foreign_and_unverifiable_policies_plan_no_op_and_warn_as_the_pass_does() {
    let mut declared_model = ledgerrow_model_with_row_security(false);
    if let Some(rs) = declared_model.row_security.as_mut() {
        rs.policies.push(ferro_schema_ir::SchemaRowPolicy {
            name: "rls_ledgerrow_raw".into(),
            command: ferro_schema_ir::RowPolicyCommand::All,
            restrictive: false,
            expr: ferro_schema_ir::RowPolicyExpr::Raw {
                using: Some("ledger_id IS NOT NULL".into()),
                with_check: Some("ledger_id IS NOT NULL".into()),
            },
        });
    }
    let mut live_model = declared_model.clone();
    live_model.row_security = None;
    let live_rs = ferro_ddl_lowering::LiveRowSecurity {
        enabled: true,
        forced: false,
        policies: vec![
            live_policy("dba_fence", "(true)"),
            live_policy("rls_ledgerrow_ledger_id", CATALOG_LEDGER_EXPR),
            live_policy("rls_ledgerrow_raw", "(ledger_id IS NULL)"),
        ],
    };
    let mut facts = LiveFacts::declared();
    facts.tables.insert(
        "ledgerrow".into(),
        LiveTableFacts {
            row_security: live_rs.clone(),
            ..LiveTableFacts::default()
        },
    );
    let declared = envelope(vec![declared_model.clone()]);
    let plan = plan_from_ir(
        &envelope(vec![live_model]),
        &declared,
        Dialect::Postgres,
        &facts,
        destructive(),
    );
    assert!(plan.operations.is_empty(), "{:?}", plan.operations);
    let reconcile = ferro_ddl_lowering::plan_row_security_reconcile(
        &declared_model,
        &live_rs,
        Dialect::Postgres,
        true,
    )
    .unwrap();
    assert_eq!(reconcile.foreign, vec!["dba_fence".to_string()]);
    assert_eq!(
        reconcile.unverifiable,
        vec!["rls_ledgerrow_raw".to_string()]
    );
    assert_eq!(plan.always_warnings, reconcile.warnings);
}

#[test]
fn row_security_ops_render_byte_identical_to_the_reconcile_decision() {
    // Every category at once: missing ENABLE, a missing policy, a drifted
    // shorthand policy, an orphan, and (destructive) the FORCE teardown.
    let mut declared_model = ledgerrow_model_with_row_security(false);
    if let Some(rs) = declared_model.row_security.as_mut() {
        rs.policies.push(ferro_schema_ir::SchemaRowPolicy {
            name: "rls_ledgerrow_reader".into(),
            command: ferro_schema_ir::RowPolicyCommand::Select,
            restrictive: true,
            expr: ferro_schema_ir::RowPolicyExpr::Setting {
                column: "ledger_id".into(),
                setting: "pinch.ledger_id".into(),
            },
        });
    }
    let mut drifted = live_policy("rls_ledgerrow_ledger_id", CATALOG_LEDGER_EXPR);
    drifted.restrictive = true;
    let live_rs = ferro_ddl_lowering::LiveRowSecurity {
        enabled: false,
        forced: true,
        policies: vec![
            drifted,
            live_policy("rls_ledgerrow_gone", CATALOG_LEDGER_EXPR),
        ],
    };
    let mut live_model = declared_model.clone();
    live_model.row_security = None;
    let live = envelope(vec![live_model]);
    let declared = envelope(vec![declared_model.clone()]);
    let mut facts = LiveFacts::declared();
    facts.tables.insert(
        "ledgerrow".into(),
        LiveTableFacts {
            row_security: live_rs.clone(),
            ..LiveTableFacts::default()
        },
    );
    for options in [updates_only(), destructive()] {
        let plan = plan_from_ir(&live, &declared, Dialect::Postgres, &facts, options);
        let rendered = render_plan(&plan, &live, &declared, Dialect::Postgres).unwrap();
        let statements: Vec<String> = rendered.into_iter().flat_map(|op| op.statements).collect();
        let reconcile = ferro_ddl_lowering::plan_row_security_reconcile(
            &declared_model,
            &live_rs,
            Dialect::Postgres,
            options.destructive,
        )
        .unwrap();
        assert!(!reconcile.statements.is_empty());
        assert_eq!(statements, reconcile.statements, "{options:?}");
        assert_eq!(plan.always_warnings, reconcile.warnings, "{options:?}");
    }
}

#[test]
fn render_plan_renders_one_entry_per_op_with_its_own_statements_and_warnings() {
    let old = envelope(vec![schema_model("doc", vec![pk_col("id", "int")])]);
    let new = envelope(vec![SchemaModel {
        foreign_keys: vec![fk("owner_id", "owner", Some("CASCADE"), None)],
        ..schema_model(
            "doc",
            vec![
                pk_col("id", "int"),
                col_with_flags(
                    "owner_id",
                    "int",
                    false,
                    false,
                    false,
                    Some(serde_json::json!(1)),
                ),
            ],
        )
    }]);
    let plan = plan_from_ir(
        &old,
        &new,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
    let rendered = render_plan(&plan, &old, &new, Dialect::Sqlite).unwrap();
    assert_eq!(rendered.len(), 1);
    assert_eq!(
        rendered[0].op,
        MigrationOp::AddColumn {
            table: "doc".into(),
            column: "owner_id".into(),
        }
    );
    assert_eq!(
        rendered[0].statements.len(),
        1,
        "{:?}",
        rendered[0].statements
    );
    assert_eq!(
        rendered[0].warnings.len(),
        1,
        "the SQLite FK skip rides its op"
    );
}

#[test]
fn ops_serialize_with_their_kind_and_fields_and_facts_round_trip() {
    let op = MigrationOp::AddColumn {
        table: "doc".into(),
        column: "title".into(),
    };
    assert_eq!(
        serde_json::to_value(&op).unwrap(),
        serde_json::json!({"kind": "AddColumn", "table": "doc", "column": "title"})
    );
    let facts = ledgerrow_with_leftovers().1;
    let round_trip: LiveFacts =
        serde_json::from_value(serde_json::to_value(&facts).unwrap()).unwrap();
    assert_eq!(round_trip, facts);
}

/// `table` with an FK to each of `targets`, before and after gaining a
/// nullable `note` column.
fn growing_table(table: &str, targets: &[&str]) -> (SchemaModel, SchemaModel) {
    let mut cols = vec![pk_col("id", "int")];
    let fks: Vec<SchemaForeignKey> = targets
        .iter()
        .map(|to| {
            cols.push(col(&format!("{to}_id"), "int", true));
            fk(&format!("{to}_id"), to, Some("CASCADE"), None)
        })
        .collect();
    let before = SchemaModel {
        foreign_keys: fks,
        ..schema_model(table, cols)
    };
    let mut after = before.clone();
    after.columns.push(col("note", "text", true));
    (before, after)
}

fn tables_in_plan_order(plan: &MigrationPlan) -> Vec<String> {
    let mut tables: Vec<String> = Vec::new();
    for op in &plan.operations {
        if let Some(table) = op.table()
            && tables.last().map(String::as_str) != Some(table)
        {
            tables.push(table.to_string());
        }
    }
    tables
}

#[test]
fn existing_tables_follow_the_tables_their_foreign_keys_reference() {
    // Sorted by model name first (`child` < `grandparent` < `parent`), then
    // so each follows the tables its FKs reference.
    let pairs = [
        growing_table("child", &["parent"]),
        growing_table("parent", &["grandparent"]),
        growing_table("grandparent", &[]),
    ];
    let old = envelope(pairs.iter().map(|(before, _)| before.clone()).collect());
    let new = envelope(pairs.iter().map(|(_, after)| after.clone()).collect());
    let plan = plan_from_ir(
        &old,
        &new,
        Dialect::Postgres,
        &LiveFacts::declared(),
        destructive(),
    );
    assert_eq!(
        tables_in_plan_order(&plan),
        vec!["grandparent", "parent", "child"]
    );
}

#[test]
fn a_self_reference_does_not_constrain_existing_table_order() {
    // #302: `znode` references itself and `areferrer` (first by name)
    // references it; the self-loop must not evict it from dependency order.
    let pairs = [
        growing_table("znode", &["znode"]),
        growing_table("areferrer", &["znode"]),
        growing_table("external_child", &["not_modeled"]),
    ];
    let old = envelope(pairs.iter().map(|(before, _)| before.clone()).collect());
    let new = envelope(pairs.iter().map(|(_, after)| after.clone()).collect());
    let plan = plan_from_ir(
        &old,
        &new,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
    assert_eq!(
        tables_in_plan_order(&plan),
        vec!["external_child", "znode", "areferrer"],
        "a reference outside the modelset constrains nothing either"
    );
}

#[test]
fn a_foreign_key_cycle_keeps_every_table_in_name_order() {
    let pairs = [
        growing_table("beta", &["alpha"]),
        growing_table("alpha", &["beta"]),
    ];
    let old = envelope(pairs.iter().map(|(before, _)| before.clone()).collect());
    let new = envelope(pairs.iter().map(|(_, after)| after.clone()).collect());
    let plan = plan_from_ir(
        &old,
        &new,
        Dialect::Sqlite,
        &LiveFacts::declared(),
        destructive(),
    );
    assert_eq!(tables_in_plan_order(&plan), vec!["alpha", "beta"]);
}

#[test]
fn plan_from_ir_plans_a_primary_key_moving_between_columns() {
    let keyed_on_id = envelope(vec![schema_model(
        "doc",
        vec![pk_col("id", "integer"), col("slug", "text", false)],
    )]);
    let keyed_on_slug = envelope(vec![schema_model(
        "doc",
        vec![
            col("id", "integer", false),
            SchemaColumn {
                primary_key: true,
                ..col("slug", "text", false)
            },
        ],
    )]);
    let change = MigrationOp::ChangePrimaryKey {
        table: "doc".to_string(),
        from: vec!["id".to_string()],
        to: vec!["slug".to_string()],
    };
    for dialect in [Dialect::Postgres, Dialect::Sqlite] {
        let plan = plan_from_ir(
            &keyed_on_id,
            &keyed_on_slug,
            dialect,
            &LiveFacts::declared(),
            destructive(),
        );
        assert_eq!(plan.operations.first(), Some(&change), "{dialect:?}");
        // The pass warns and skips: no statement, one warning naming the
        // reviewed-migration door.
        let rendered = render_plan(&plan, &keyed_on_id, &keyed_on_slug, dialect).expect("render");
        let op = rendered.iter().find(|r| r.op == change).expect("rendered");
        assert!(op.statements.is_empty());
        assert_eq!(
            op.warnings,
            [
                "Table 'doc' declares primary key (slug) but its primary key is (id). A \
                 primary key cannot be changed in place, so the live key remains; generate \
                 a reviewed migration with `ferro migrate new`."
            ]
        );
        let unchanged = plan_from_ir(
            &keyed_on_id,
            &keyed_on_id,
            dialect,
            &LiveFacts::declared(),
            destructive(),
        );
        assert!(
            unchanged.operations.is_empty(),
            "{:?}",
            unchanged.operations
        );
    }
    // The same key listed in another column order is no change: a live table
    // reports catalog order.
    let composite = |cols: [&str; 2]| {
        envelope(vec![schema_model(
            "pair",
            cols.iter().map(|name| pk_col(name, "integer")).collect(),
        )])
    };
    let plan = plan_from_ir(
        &composite(["a", "b"]),
        &composite(["b", "a"]),
        Dialect::Postgres,
        &LiveFacts::declared(),
        destructive(),
    );
    assert!(
        !plan
            .operations
            .iter()
            .any(|op| matches!(op, MigrationOp::ChangePrimaryKey { .. })),
        "{:?}",
        plan.operations
    );
}

// ---------------------------------------------------------------------------
// Online shapes on an existing Postgres table (#527; ADR-0043, ADR-0044): the
// pass's own renderers in a mode, one token apart (AGENTS.md § I-1).
// ---------------------------------------------------------------------------

#[test]
fn the_concurrent_index_differs_from_the_passs_by_exactly_the_concurrently_token() {
    use ferro_ddl_lowering::IndexMode;
    for unique in [false, true] {
        let columns = ["email".to_string()];
        let plain = crate::emit::render_index_sql(
            "author",
            "uq_author_email",
            &columns,
            unique,
            Dialect::Postgres,
            IndexMode::Plain,
        );
        let concurrent = crate::emit::render_index_sql(
            "author",
            "uq_author_email",
            &columns,
            unique,
            Dialect::Postgres,
            IndexMode::Concurrent,
        );
        let tokens = |sql: &str| sql.split(' ').map(str::to_string).collect::<Vec<_>>();
        let (plain_tokens, concurrent_tokens) = (tokens(&plain), tokens(&concurrent));
        let head = if unique { 3 } else { 2 };
        assert_eq!(plain_tokens[..head], concurrent_tokens[..head]);
        assert_eq!(plain_tokens[head..head + 3], ["IF", "NOT", "EXISTS"]);
        assert_eq!(concurrent_tokens[head], "CONCURRENTLY");
        assert_eq!(plain_tokens[head + 3..], concurrent_tokens[head + 1..]);
        assert_eq!(
            concurrent,
            format!(
                "CREATE {}INDEX CONCURRENTLY \"uq_author_email\" ON \"author\" (\"email\")",
                if unique { "UNIQUE " } else { "" }
            )
        );
    }
}

#[test]
fn a_not_valid_foreign_key_is_the_passs_add_plus_the_one_token() {
    use ferro_ddl_lowering::ConstraintMode;
    let model = post_model_with_constraints();
    let fk = &model.foreign_keys[0];
    let plain = crate::emit::render_add_fk_sql("post", fk, ConstraintMode::Plain);
    assert_eq!(
        crate::emit::render_add_fk_sql("post", fk, ConstraintMode::NotValid),
        format!("{plain} NOT VALID")
    );
}

#[test]
fn the_passs_index_statements_are_byte_unchanged_by_the_modes() {
    let old_ir = envelope(vec![schema_model(
        "post",
        vec![pk_col("id", "int"), col("slug", "text", true)],
    )]);
    let new_ir = envelope(vec![post_model_with_constraints()]);
    let plan = plan_from_ir(
        &old_ir,
        &new_ir,
        Dialect::Postgres,
        &LiveFacts::declared(),
        PlanOptions { destructive: true },
    );
    let pg = render_flat(&plan, &old_ir, &new_ir, Dialect::Postgres).unwrap();
    assert!(
        pg.statements.contains(
            &"CREATE UNIQUE INDEX IF NOT EXISTS \"uq_post_slug\" ON \"post\" (\"slug\")"
                .to_string()
        ),
        "{:?}",
        pg.statements
    );
    assert!(
        !pg.statements
            .iter()
            .any(|sql| sql.contains("CONCURRENTLY") || sql.contains("NOT VALID"))
    );
}

// -- rename hints (#528, ADR-0032) ----------------------------------------------------

mod renames {
    use super::*;
    use crate::plan::{Hint, HintError, live_hints};

    fn fk(column: &str, table: &str, to_table: &str) -> SchemaForeignKey {
        SchemaForeignKey {
            column: column.to_string(),
            to_table: to_table.to_string(),
            to_column: "id".to_string(),
            on_delete: Some("CASCADE".to_string()),
            name: Some(ferro_ddl_lowering::fk_name(table, column, to_table)),
            renamed_from: None,
        }
    }

    /// `writer(id, name idx, genre ck)` and `book(id, writer_id → writer)`.
    fn parent() -> IrEnvelope<SchemaIrPayload> {
        let mut writer = schema_model(
            "writer",
            vec![
                pk_col("id", "integer"),
                col_with_flags("name", "text", false, false, true, None),
                col("genre", "text", false),
            ],
        );
        writer.indexes = vec![SchemaIndex {
            name: "idx_writer_name".to_string(),
            columns: vec!["name".to_string()],
            unique: false,
        }];
        writer.checks = vec![SchemaCheck {
            name: "ck_writer_genre".to_string(),
            column: "genre".to_string(),
            values: vec!["'novel'".to_string(), "'poem'".to_string()],
        }];
        let mut book = schema_model(
            "book",
            vec![pk_col("id", "integer"), col("writer_id", "integer", false)],
        );
        book.foreign_keys = vec![fk("writer_id", "book", "writer")];
        envelope(vec![book, writer])
    }

    /// The same schema with `writer` declared as `author`
    /// (`__ferro_renamed_from__ = "writer"`) and `name` as `full_name`
    /// (`renamed_from="name"`).
    fn target() -> IrEnvelope<SchemaIrPayload> {
        let mut author = schema_model(
            "author",
            vec![
                pk_col("id", "integer"),
                SchemaColumn {
                    renamed_from: Some("name".to_string()),
                    ..col_with_flags("full_name", "text", false, false, true, None)
                },
                col("genre", "text", false),
            ],
        );
        author.renamed_from = Some("writer".to_string());
        author.indexes = vec![SchemaIndex {
            name: "idx_author_full_name".to_string(),
            columns: vec!["full_name".to_string()],
            unique: false,
        }];
        author.checks = vec![SchemaCheck {
            name: "ck_author_genre".to_string(),
            column: "genre".to_string(),
            values: vec!["'novel'".to_string(), "'poem'".to_string()],
        }];
        let mut book = schema_model(
            "book",
            vec![pk_col("id", "integer"), col("writer_id", "integer", false)],
        );
        book.foreign_keys = vec![fk("writer_id", "book", "author")];
        envelope(vec![author, book])
    }

    fn plan(
        old: &IrEnvelope<SchemaIrPayload>,
        new: &IrEnvelope<SchemaIrPayload>,
        dialect: Dialect,
    ) -> Vec<MigrationOp> {
        plan_from_ir(
            old,
            new,
            dialect,
            &LiveFacts::declared(),
            PlanOptions { destructive: true },
        )
        .operations
    }

    #[test]
    fn a_refused_hint_renames_nothing_and_stands_as_a_warning_naming_both() {
        let mut twice = target();
        twice.payload.models[0].columns.push(SchemaColumn {
            renamed_from: Some("name".to_string()),
            ..col("display_name", "text", true)
        });
        let plan = plan_from_ir(
            &parent(),
            &twice,
            Dialect::Postgres,
            &LiveFacts::declared(),
            PlanOptions { destructive: true },
        );
        assert!(
            !plan.operations.iter().any(|op| matches!(
                op,
                MigrationOp::RenameTable { .. } | MigrationOp::RenameColumn { .. }
            )),
            "{:?}",
            plan.operations
        );
        let warning = plan
            .always_warnings
            .iter()
            .find(|w| w.starts_with("rename hint refused"))
            .expect("the refusal stands");
        assert!(warning.contains("author.full_name") && warning.contains("author.display_name"));
    }

    #[test]
    fn a_plan_without_hints_is_unchanged_by_the_hint_pass() {
        // The reconciliation pass's shape: a live `old` and a modelset that
        // declares no hint plan exactly as before (#528).
        let mut no_hints = target();
        no_hints.payload.models[0].renamed_from = None;
        no_hints.payload.models[0].columns[1].renamed_from = None;
        let ops = plan(&parent(), &no_hints, Dialect::Postgres);
        assert!(ops.contains(&MigrationOp::AddTable {
            table: "author".to_string()
        }));
        assert!(ops.contains(&MigrationOp::DropTable {
            table: "writer".to_string()
        }));
        assert!(
            !ops.iter()
                .any(|op| matches!(op, MigrationOp::RenameTable { .. }))
        );
    }

    #[test]
    fn a_hint_is_live_while_the_parent_holds_the_old_name_and_lacks_the_new_one() {
        assert_eq!(
            live_hints(&parent().payload, &target().payload).expect("no refusal"),
            vec![
                Hint::Table {
                    old: "writer".to_string(),
                    new: "author".to_string(),
                },
                // A column hint inside a renamed table resolves against the
                // old table.
                Hint::Column {
                    table: "author".to_string(),
                    old: "name".to_string(),
                    new: "full_name".to_string(),
                },
            ]
        );
        // After its migration the parent holds the new names: inert.
        assert_eq!(
            live_hints(&target().payload, &target().payload).expect("no refusal"),
            vec![]
        );
        // A hint naming something neither side holds is inert and silent.
        let mut stray = target();
        stray.payload.models[0].renamed_from = Some("scribe".to_string());
        stray.payload.models[0].columns[1].renamed_from = Some("moniker".to_string());
        assert_eq!(
            live_hints(&target().payload, &stray.payload).expect("no refusal"),
            vec![]
        );
    }

    #[test]
    fn a_hint_whose_old_name_is_still_declared_is_refused_naming_both() {
        let mut still = target();
        still.payload.models[0]
            .columns
            .push(col("name", "text", false));
        let err = live_hints(&parent().payload, &still.payload).expect_err("refused");
        assert_eq!(
            err,
            HintError::OldStillDeclared {
                table: "author".to_string(),
                field: Some("full_name".to_string()),
                old: "name".to_string(),
            }
        );
        let message = err.to_string();
        assert!(message.contains("author.full_name"), "{message}");
        assert!(message.contains("renamed_from=\"name\""), "{message}");

        let mut table_still = target();
        table_still
            .payload
            .models
            .push(schema_model("writer", vec![pk_col("id", "integer")]));
        let err = live_hints(&parent().payload, &table_still.payload).expect_err("refused");
        assert_eq!(
            err,
            HintError::OldStillDeclared {
                table: "author".to_string(),
                field: None,
                old: "writer".to_string(),
            }
        );
    }

    #[test]
    fn two_hints_claiming_one_old_name_are_refused_naming_both() {
        let mut twice = target();
        twice.payload.models[0].columns.push(SchemaColumn {
            renamed_from: Some("name".to_string()),
            ..col("display_name", "text", true)
        });
        let err = live_hints(&parent().payload, &twice.payload).expect_err("refused");
        assert_eq!(
            err,
            HintError::Ambiguous {
                table: Some("author".to_string()),
                old: "name".to_string(),
                claimants: vec!["full_name".to_string(), "display_name".to_string()],
            }
        );
        let message = err.to_string();
        assert!(message.contains("author.full_name"), "{message}");
        assert!(message.contains("author.display_name"), "{message}");
    }

    #[test]
    fn a_table_rename_drags_every_derived_name_in_one_ordered_set() {
        let expected = vec![
            MigrationOp::RenameTable {
                old: "writer".to_string(),
                new: "author".to_string(),
            },
            MigrationOp::RenameColumn {
                table: "author".to_string(),
                old: "name".to_string(),
                new: "full_name".to_string(),
            },
            MigrationOp::RenameIndex {
                table: "author".to_string(),
                old: "idx_writer_name".to_string(),
                new: "idx_author_full_name".to_string(),
            },
            MigrationOp::RenameConstraint {
                table: "author".to_string(),
                old: "ck_writer_genre".to_string(),
                new: "ck_author_genre".to_string(),
            },
            MigrationOp::RenameConstraint {
                table: "book".to_string(),
                old: "fk_book_writer_id_writer".to_string(),
                new: "fk_book_writer_id_author".to_string(),
            },
        ];
        assert_eq!(plan(&parent(), &target(), Dialect::Postgres), expected);
        assert_eq!(plan(&parent(), &target(), Dialect::Sqlite), expected);
    }

    #[test]
    fn live_facts_follow_the_renames_so_the_pass_plans_only_the_renames() {
        // The reconciliation pass's shape: facts read from the live database
        // before the renames run, keyed and named the old way.
        let mut facts = LiveFacts::declared();
        facts.tables.insert(
            "writer".to_string(),
            crate::plan::LiveTableFacts {
                checks: vec![crate::plan::LiveCheckFact {
                    name: "ck_writer_genre".to_string(),
                    definition: "CHECK (\"genre\" IN ('novel', 'poem'))".to_string(),
                    ferro_owned: true,
                    validated: true,
                }],
                ..Default::default()
            },
        );
        // A live IR carries no check: only the facts know `ck_writer_genre`.
        let mut live = parent();
        for model in &mut live.payload.models {
            model.checks.clear();
        }
        let ops = plan_from_ir(
            &live,
            &target(),
            Dialect::Postgres,
            &facts,
            PlanOptions { destructive: true },
        )
        .operations;
        assert!(
            ops.contains(&MigrationOp::RenameConstraint {
                table: "author".to_string(),
                old: "ck_writer_genre".to_string(),
                new: "ck_author_genre".to_string(),
            }),
            "{ops:?}"
        );
        assert!(
            ops.iter().all(|op| matches!(
                op,
                MigrationOp::RenameTable { .. }
                    | MigrationOp::RenameColumn { .. }
                    | MigrationOp::RenameIndex { .. }
                    | MigrationOp::RenameConstraint { .. }
            )),
            "{ops:?}"
        );
    }

    #[test]
    fn a_live_check_that_drifted_before_the_rename_is_rebuilt_in_the_same_plan() {
        // Equal to the declaration under the old names: read as renamed, no
        // rebuild (pinned above). Unequal: kept as read, so the one drift
        // decision rebuilds it now, not on the next run.
        let mut facts = LiveFacts::declared();
        facts.tables.insert(
            "writer".to_string(),
            crate::plan::LiveTableFacts {
                checks: vec![crate::plan::LiveCheckFact {
                    name: "ck_writer_genre".to_string(),
                    definition: "CHECK (\"genre\" IN ('novel'))".to_string(),
                    ferro_owned: true,
                    validated: true,
                }],
                ..Default::default()
            },
        );
        let mut live = parent();
        for model in &mut live.payload.models {
            model.checks.clear();
        }
        let ops = plan_from_ir(
            &live,
            &target(),
            Dialect::Postgres,
            &facts,
            PlanOptions { destructive: true },
        )
        .operations;
        let rename = ops
            .iter()
            .position(|op| {
                op == &MigrationOp::RenameConstraint {
                    table: "author".to_string(),
                    old: "ck_writer_genre".to_string(),
                    new: "ck_author_genre".to_string(),
                }
            })
            .expect("the rename");
        let rebuild = ops
            .iter()
            .position(|op| {
                op == &MigrationOp::RebuildCheck {
                    table: "author".to_string(),
                    name: "ck_author_genre".to_string(),
                }
            })
            .expect("the drifted body is rebuilt in this plan");
        assert!(rename < rebuild, "{ops:?}");
    }

    #[test]
    fn each_tables_renames_are_one_contiguous_unit_naming_the_table() {
        // The reconciliation pass runs a table's consecutive ops in one
        // transaction: no rename op may be table-less, and one table's may
        // not be split by another's.
        let ops = plan(&parent(), &target(), Dialect::Postgres);
        let tables: Vec<&str> = ops
            .iter()
            .map(|op| op.table().expect("every rename op names its table"))
            .collect();
        let mut seen: Vec<&str> = Vec::new();
        for table in &tables {
            if seen.last() != Some(table) {
                assert!(!seen.contains(table), "{table} split: {tables:?}");
                seen.push(table);
            }
        }
        assert_eq!(seen, ["author", "book"]);
    }

    #[test]
    fn a_rename_and_a_type_change_on_one_column_plan_the_rename_first() {
        let mut changed = target();
        changed.payload.models[0].columns[1].db_type = Some("varchar(80)".to_string());
        let ops = plan(&parent(), &changed, Dialect::Postgres);
        let type_change = MigrationOp::AlterColumnType {
            table: "author".to_string(),
            column: "full_name".to_string(),
        };
        assert_eq!(ops.last(), Some(&type_change), "{ops:?}");
        assert!(matches!(ops[0], MigrationOp::RenameTable { .. }), "{ops:?}");
    }

    #[test]
    fn an_inert_hint_plans_nothing() {
        assert_eq!(plan(&target(), &target(), Dialect::Postgres), vec![]);
        let mut deleted = target();
        deleted.payload.models[0].renamed_from = None;
        deleted.payload.models[0].columns[1].renamed_from = None;
        assert_eq!(plan(&target(), &deleted, Dialect::Postgres), vec![]);
    }

    #[test]
    fn the_rename_ops_render_through_the_one_renderer_per_statement() {
        let ops = plan(&parent(), &target(), Dialect::Postgres);
        let rendered = render_plan(
            &MigrationPlan {
                operations: ops,
                ..MigrationPlan::default()
            },
            &parent(),
            &target(),
            Dialect::Postgres,
        )
        .expect("renders");
        let statements: Vec<String> = rendered.into_iter().flat_map(|op| op.statements).collect();
        assert_eq!(
            statements,
            [
                "ALTER TABLE \"writer\" RENAME TO \"author\"",
                "ALTER TABLE \"author\" RENAME COLUMN \"name\" TO \"full_name\"",
                "ALTER INDEX \"idx_writer_name\" RENAME TO \"idx_author_full_name\"",
                "ALTER TABLE \"author\" RENAME CONSTRAINT \"ck_writer_genre\" TO \"ck_author_genre\"",
                "ALTER TABLE \"book\" RENAME CONSTRAINT \"fk_book_writer_id_writer\" TO \
                 \"fk_book_writer_id_author\"",
            ]
        );

        // SQLite has no index rename: the index is dropped and created under
        // its new name, by the statements every door uses.
        let index_only = MigrationPlan {
            operations: vec![MigrationOp::RenameIndex {
                table: "author".to_string(),
                old: "idx_writer_name".to_string(),
                new: "idx_author_full_name".to_string(),
            }],
            ..MigrationPlan::default()
        };
        let sqlite = render_plan(&index_only, &parent(), &target(), Dialect::Sqlite)
            .expect("renders")
            .remove(0)
            .statements;
        assert_eq!(
            sqlite,
            [
                "DROP INDEX IF EXISTS \"idx_writer_name\"",
                "CREATE INDEX IF NOT EXISTS \"idx_author_full_name\" ON \"author\" (\"full_name\")",
            ]
        );
    }
}

/// Enum label renames (`__ferro_renamed_labels__`) and inferred enum type
/// renames (ADR-0032; #529).
mod enum_renames {
    use super::*;
    use crate::plan::{Hint, HintError, live_hints};
    use std::collections::BTreeMap;

    fn status(type_name: &str, labels: &[&str], hints: &[(&str, &str)]) -> SchemaColumn {
        SchemaColumn {
            enum_values: Some(labels.iter().map(|l| serde_json::json!(l)).collect()),
            enum_type_name: Some(type_name.to_string()),
            db_type: None,
            enum_renamed_labels: (!hints.is_empty()).then(|| {
                ferro_schema_ir::SchemaRenamedLabels {
                    enum_class: "OrderStatus".to_string(),
                    labels: hints
                        .iter()
                        .map(|(new, old)| (new.to_string(), old.to_string()))
                        .collect::<BTreeMap<_, _>>(),
                }
            }),
            ..col("status", "text", false)
        }
    }

    /// `enmorder(id, status <type>)` and `enmrefund(id, status <type>)`.
    fn orders(columns: [SchemaColumn; 2]) -> IrEnvelope<SchemaIrPayload> {
        let [order, refund] = columns;
        envelope(vec![
            schema_model("enmorder", vec![pk_col("id", "integer"), order]),
            schema_model("enmrefund", vec![pk_col("id", "integer"), refund]),
        ])
    }

    fn both(column: SchemaColumn) -> IrEnvelope<SchemaIrPayload> {
        orders([column.clone(), column])
    }

    fn plan(
        old: &IrEnvelope<SchemaIrPayload>,
        new: &IrEnvelope<SchemaIrPayload>,
        dialect: Dialect,
    ) -> MigrationPlan {
        plan_from_ir(
            old,
            new,
            dialect,
            &LiveFacts::declared(),
            PlanOptions { destructive: true },
        )
    }

    fn columns() -> Vec<(String, String)> {
        vec![
            ("enmorder".to_string(), "status".to_string()),
            ("enmrefund".to_string(), "status".to_string()),
        ]
    }

    fn parent() -> IrEnvelope<SchemaIrPayload> {
        both(status("orderstatus", &["paid", "canceled"], &[]))
    }

    fn relabelled() -> IrEnvelope<SchemaIrPayload> {
        both(status(
            "orderstatus",
            &["paid", "cancelled"],
            &[("cancelled", "canceled")],
        ))
    }

    fn renames_a_type(ops: &[MigrationOp]) -> bool {
        ops.iter()
            .any(|op| matches!(op, MigrationOp::RenameEnumType { .. }))
    }

    #[test]
    fn a_live_label_hint_plans_one_rename_over_every_column_of_the_type() {
        assert_eq!(
            live_hints(&parent().payload, &relabelled().payload).expect("no refusal"),
            vec![Hint::Label {
                type_name: "orderstatus".to_string(),
                old: "canceled".to_string(),
                new: "cancelled".to_string(),
            }]
        );
        // On SQLite the column is text as wide as its longest label: the
        // longer spelling widens every column of the type too.
        let widened = || {
            columns()
                .into_iter()
                .map(|(table, column)| MigrationOp::AlterColumnType { table, column })
        };
        for dialect in [Dialect::Postgres, Dialect::Sqlite] {
            let plan = plan(&parent(), &relabelled(), dialect);
            let mut expected = vec![MigrationOp::RenameEnumLabel {
                type_name: "orderstatus".to_string(),
                old: "canceled".to_string(),
                new: "cancelled".to_string(),
                columns: columns(),
            }];
            if dialect == Dialect::Sqlite {
                expected.extend(widened());
            }
            assert_eq!(plan.operations, expected, "{dialect:?}");
            assert!(plan.warnings.is_empty(), "{:?}", plan.warnings);
            assert!(
                plan.always_warnings.is_empty(),
                "{:?}",
                plan.always_warnings
            );
        }
    }

    #[test]
    fn a_label_rename_renders_rename_value_on_postgres_and_an_update_per_column_on_sqlite() {
        let pg = render_flat(
            &plan(&parent(), &relabelled(), Dialect::Postgres),
            &parent(),
            &relabelled(),
            Dialect::Postgres,
        )
        .expect("renders");
        assert_eq!(
            pg.statements,
            ["ALTER TYPE \"orderstatus\" RENAME VALUE 'canceled' TO 'cancelled'"]
        );
        let sqlite = render_flat(
            &plan(&parent(), &relabelled(), Dialect::Sqlite),
            &parent(),
            &relabelled(),
            Dialect::Sqlite,
        )
        .expect("renders");
        assert_eq!(
            sqlite.statements,
            [
                "UPDATE \"enmorder\" SET \"status\" = 'cancelled' WHERE \"status\" = 'canceled'",
                "UPDATE \"enmrefund\" SET \"status\" = 'cancelled' WHERE \"status\" = 'canceled'",
            ]
        );
    }

    #[test]
    fn a_label_hint_is_inert_once_the_parent_holds_the_new_label() {
        assert_eq!(
            live_hints(&relabelled().payload, &relabelled().payload).expect("no refusal"),
            vec![]
        );
        assert!(plan(&relabelled(), &relabelled(), Dialect::Postgres).is_empty());
        // A hint naming a label neither side holds is inert and silent.
        let stray = both(status(
            "orderstatus",
            &["paid", "canceled"],
            &[("void", "nulled")],
        ));
        assert_eq!(
            live_hints(&parent().payload, &stray.payload).expect("no refusal"),
            vec![]
        );
    }

    #[test]
    fn a_label_hint_whose_old_label_is_still_declared_is_refused_naming_it() {
        let still = both(status(
            "orderstatus",
            &["paid", "canceled", "cancelled"],
            &[("cancelled", "canceled")],
        ));
        let err = live_hints(&parent().payload, &still.payload).expect_err("refused");
        assert_eq!(
            err,
            HintError::LabelStillDeclared {
                enum_class: "OrderStatus".to_string(),
                type_name: "orderstatus".to_string(),
                new: "cancelled".to_string(),
                old: "canceled".to_string(),
            }
        );
        assert_eq!(
            err.to_string(),
            "enum OrderStatus (type \"orderstatus\") declares __ferro_renamed_labels__ \
             {\"cancelled\": \"canceled\"}, but OrderStatus still declares the label \
             \"canceled\": a label cannot be renamed from one the enum keeps; delete the hint \
             or the old member"
        );
        // Checked live or not: the parent need not hold either label.
        assert!(live_hints(&still.payload, &still.payload).is_err());
    }

    #[test]
    fn two_labels_renamed_from_one_are_refused_naming_both() {
        let twice = both(status(
            "orderstatus",
            &["paid", "cancelled", "voided"],
            &[("cancelled", "canceled"), ("voided", "canceled")],
        ));
        let err = live_hints(&parent().payload, &twice.payload).expect_err("refused");
        assert_eq!(
            err.to_string(),
            "enum OrderStatus (type \"orderstatus\") declares labels \"cancelled\" and \"voided\" all \
             renamed from \"canceled\": one label becomes one label; keep the hint on the \
             label \"canceled\" became"
        );
    }

    #[test]
    fn every_column_moved_to_one_new_type_is_a_type_rename_on_postgres() {
        let renamed = both(status("orderstate", &["paid", "canceled"], &[]));
        let pg = plan(&parent(), &renamed, Dialect::Postgres);
        assert_eq!(
            pg.operations,
            vec![MigrationOp::RenameEnumType {
                old: "orderstatus".to_string(),
                new: "orderstate".to_string(),
            }]
        );
        assert!(pg.warnings.is_empty(), "{:?}", pg.warnings);
        assert_eq!(
            render_flat(&pg, &parent(), &renamed, Dialect::Postgres)
                .expect("renders")
                .statements,
            ["ALTER TYPE \"orderstatus\" RENAME TO \"orderstate\""]
        );
        // SQLite has no enum types: nothing to do.
        assert!(plan(&parent(), &renamed, Dialect::Sqlite).is_empty());
        // The way back is the reverse rename.
        assert_eq!(
            plan(&renamed, &parent(), Dialect::Postgres).operations,
            vec![MigrationOp::RenameEnumType {
                old: "orderstate".to_string(),
                new: "orderstatus".to_string(),
            }]
        );
    }

    #[test]
    fn a_type_some_of_whose_columns_moved_is_no_rename_but_a_type_change() {
        let split = orders([
            status("orderstate", &["paid", "canceled"], &[]),
            status("orderstatus", &["paid", "canceled"], &[]),
        ]);
        let ops = plan(&parent(), &split, Dialect::Postgres).operations;
        assert!(!renames_a_type(&ops), "{ops:?}");
        assert_eq!(
            ops,
            vec![MigrationOp::AlterColumnType {
                table: "enmorder".to_string(),
                column: "status".to_string(),
            }]
        );
        // Columns moving to a type that already exists, or to two new types,
        // are no rename either.
        let existing = orders([
            status("orderstatus", &["paid", "canceled"], &[]),
            status("refundstatus", &["paid", "canceled"], &[]),
        ]);
        let moved_to_existing = orders([
            status("refundstatus", &["paid", "canceled"], &[]),
            status("refundstatus", &["paid", "canceled"], &[]),
        ]);
        let ops = plan(&existing, &moved_to_existing, Dialect::Postgres).operations;
        assert!(!renames_a_type(&ops), "{ops:?}");
        let two_types = orders([
            status("orderstate", &["paid", "canceled"], &[]),
            status("orderphase", &["paid", "canceled"], &[]),
        ]);
        let ops = plan(&parent(), &two_types, Dialect::Postgres).operations;
        assert!(!renames_a_type(&ops), "{ops:?}");
        // A column of the vanished type dropped in the same edit: not every
        // column moved.
        let one_dropped = envelope(vec![
            schema_model(
                "enmorder",
                vec![
                    pk_col("id", "integer"),
                    status("orderstate", &["paid", "canceled"], &[]),
                ],
            ),
            schema_model("enmrefund", vec![pk_col("id", "integer")]),
        ]);
        let ops = plan(&parent(), &one_dropped, Dialect::Postgres).operations;
        assert!(!renames_a_type(&ops), "{ops:?}");
    }

    #[test]
    fn a_type_rename_and_a_label_rename_in_one_edit_rename_the_type_first() {
        let both_renamed = both(status(
            "orderstate",
            &["paid", "cancelled"],
            &[("cancelled", "canceled")],
        ));
        let expected_type = MigrationOp::RenameEnumType {
            old: "orderstatus".to_string(),
            new: "orderstate".to_string(),
        };
        let expected_label = MigrationOp::RenameEnumLabel {
            type_name: "orderstate".to_string(),
            old: "canceled".to_string(),
            new: "cancelled".to_string(),
            columns: columns(),
        };
        assert_eq!(
            plan(&parent(), &both_renamed, Dialect::Postgres).operations,
            vec![expected_type, expected_label.clone()]
        );
        let mut sqlite = vec![expected_label];
        sqlite.extend(
            columns()
                .into_iter()
                .map(|(table, column)| MigrationOp::AlterColumnType { table, column }),
        );
        assert_eq!(
            plan(&parent(), &both_renamed, Dialect::Sqlite).operations,
            sqlite
        );
    }

    #[test]
    fn an_added_label_beside_a_renamed_one_is_still_added() {
        let edited = both(status(
            "orderstatus",
            &["paid", "cancelled", "refunded"],
            &[("cancelled", "canceled")],
        ));
        assert_eq!(
            plan(&parent(), &edited, Dialect::Postgres).operations,
            vec![
                MigrationOp::RenameEnumLabel {
                    type_name: "orderstatus".to_string(),
                    old: "canceled".to_string(),
                    new: "cancelled".to_string(),
                    columns: columns(),
                },
                MigrationOp::AddEnumLabel {
                    type_name: "orderstatus".to_string(),
                    label: "refunded".to_string(),
                },
            ]
        );
    }
}
