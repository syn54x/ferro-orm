//! `plan_revision`: the four rules, the downgrade's irreversible reasons and
//! the dropped table, without a database: a live side and the models, in
//! memory.

use super::*;
use crate::plan::{LiveCheckFact, LiveFacts, LiveTableFacts};
use ferro_ddl_lowering::{LiveRowPolicy, LiveRowSecurity, Subject};
use ferro_schema_ir::{
    RowPolicyCommand, RowPolicyExpr, SchemaCheck, SchemaColumn, SchemaForeignKey, SchemaIndex,
    SchemaRowPolicy, SchemaRowSecurity,
};

fn envelope(models: Vec<SchemaModel>) -> IrEnvelope<SchemaIrPayload> {
    IrEnvelope {
        ir_kind: "schema".into(),
        ir_version: 2,
        payload: SchemaIrPayload {
            dialect_agnostic: true,
            models,
        },
    }
}

fn column(name: &str, db_type: &str, nullable: bool) -> SchemaColumn {
    SchemaColumn {
        renamed_from: None,
        name: name.into(),
        logical_type: "unknown".into(),
        db_type: Some(db_type.into()),
        db_type_explicit: None,
        nullable,
        primary_key: name == "id",
        autoincrement: false,
        unique: false,
        index: false,
        default: None,
        default_factory: None,
        format: None,
        enum_values: None,
        enum_type_name: None,
        postgres_native_enum: false,
        enum_renamed_labels: Default::default(),
    }
}

/// A native enum column of the type `type_name` holding `labels`, as the
/// live database (`live`) or the models declare it.
fn enum_column(name: &str, type_name: &str, labels: &[&str], live: bool) -> SchemaColumn {
    SchemaColumn {
        logical_type: "string".into(),
        db_type: None,
        enum_values: Some(
            labels
                .iter()
                .map(|label| serde_json::json!(label))
                .collect(),
        ),
        enum_type_name: Some(type_name.into()),
        postgres_native_enum: live,
        ..column(name, type_name, false)
    }
}

fn table(name: &str, columns: Vec<SchemaColumn>) -> SchemaModel {
    SchemaModel {
        renamed_from: None,
        model_name: format!("app.{name}"),
        table_name: name.into(),
        columns,
        foreign_keys: Vec::new(),
        indexes: Vec::new(),
        uniques: Vec::new(),
        checks: Vec::new(),
        table_checks: Vec::new(),
        row_security: None,
    }
}

fn card(columns: Vec<SchemaColumn>) -> SchemaModel {
    table("card", columns)
}

fn id() -> SchemaColumn {
    column("id", "int", false)
}

/// The live side holding `models`, every table with no facts but `facts`.
fn live_with(
    models: Vec<SchemaModel>,
    facts: &[(&str, LiveTableFacts)],
    enum_labels: &[(&str, &[&str])],
) -> Side {
    let mut live = LiveFacts::default();
    for model in &models {
        live.tables
            .insert(model.table_name.clone(), LiveTableFacts::default());
    }
    for (table, table_facts) in facts {
        live.tables.insert((*table).into(), table_facts.clone());
    }
    for (type_name, labels) in enum_labels {
        live.enum_labels.insert(
            (*type_name).into(),
            labels.iter().map(|label| label.to_string()).collect(),
        );
    }
    Side::live(envelope(models), live).expect("live side")
}

fn live(models: Vec<SchemaModel>) -> Side {
    live_with(models, &[], &[])
}

fn kinds(written: &[RevisionOp]) -> Vec<String> {
    written.iter().map(|op| kind_name(&op.op)).collect()
}

fn flavor_check() -> SchemaCheck {
    SchemaCheck {
        name: "ck_card_flavor".into(),
        column: "flavor".into(),
        values: vec!["a".into()],
    }
}

fn flavor_check_fact() -> LiveTableFacts {
    LiveTableFacts {
        checks: vec![LiveCheckFact {
            name: "ck_card_flavor".into(),
            definition: "CHECK (flavor IN ('a'))".into(),
            ferro_owned: true,
            validated: true,
        }],
        ..LiveTableFacts::default()
    }
}

// -- the four rules ---------------------------------------------------------------------

#[test]
fn a_sqlite_rebuild_is_refused_naming_ferro_migrate_new() {
    let mut checked = card(vec![id(), column("flavor", "text", true)]);
    checked.checks.push(flavor_check());
    let refusal = plan_revision(
        &live(vec![card(vec![id(), column("flavor", "text", true)])]),
        &envelope(vec![checked]),
        Dialect::Sqlite,
    )
    .expect_err("a rebuild is refused");
    assert_eq!(
        refusal,
        RevisionRefusal::Rebuild {
            kind: "AddCheck".into(),
            subject: "card".into()
        }
    );
    assert_eq!(
        refusal.to_string(),
        "AddCheck on card needs a SQLite table rebuild, which an Alembic revision cannot write \
         (batch mode has no foreign-key pragma handling, so the drop cascades into ON DELETE \
         CASCADE children). Write this change as a migration: `ferro migrate new`"
    );
}

#[test]
fn a_demanding_column_is_the_plain_op_under_its_data_dependent_marker() {
    let before = live(vec![card(vec![id()])]);
    let after = envelope(vec![card(vec![id(), column("flavor", "text", false)])]);
    let revision = plan_revision(&before, &after, Dialect::Postgres).expect("a revision");
    let [add] = revision.upgrade.as_slice() else {
        panic!("{:?}", revision.upgrade);
    };
    assert_eq!(
        add.op,
        MigrationOp::AddColumn {
            table: "card".into(),
            column: "flavor".into()
        }
    );
    // The pass has no statement for it: Alembic's own op writes it.
    assert!(add.statements.is_empty());
    assert!(add.twin);
    assert_eq!(
        add.marker,
        Some(Marker::DataDependent(
            "data-dependent (fails while card has rows; ferro migrations generate the \
             backfill: `ferro migrate new`)"
                .into()
        ))
    );
    let [drop] = revision.downgrade.as_slice() else {
        panic!("{:?}", revision.downgrade);
    };
    assert_eq!(
        drop.statements,
        vec!["ALTER TABLE \"card\" DROP COLUMN \"flavor\"".to_string()]
    );
    // A down never carries the destructive marker (ADR-0033).
    assert_eq!(
        (drop.marker.as_ref(), drop.irreversible.as_ref()),
        (None, None)
    );

    let refusal = plan_revision(&before, &after, Dialect::Sqlite).expect_err("refused");
    assert_eq!(
        refusal.to_string(),
        "AddColumn on card.flavor adds a NOT NULL column with no value for the rows already \
         there, which SQLite cannot add in place. Give it a default, or write the change as a \
         migration, which generates the backfill: `ferro migrate new`"
    );
}

#[test]
fn a_dropped_column_is_destructive_going_up_and_comes_back_demanding_values() {
    let before = live(vec![card(vec![id(), column("flavor", "text", false)])]);
    let after = envelope(vec![card(vec![id()])]);
    let revision = plan_revision(&before, &after, Dialect::Postgres).expect("a revision");
    assert_eq!(kinds(&revision.upgrade), ["DropColumn"]);
    assert_eq!(
        revision.upgrade[0].marker,
        Some(Marker::Destructive(
            "destructive (drops card.flavor and the data it holds)".into()
        ))
    );
    let [add] = revision.downgrade.as_slice() else {
        panic!("{:?}", revision.downgrade);
    };
    assert_eq!(kind_name(&add.op), "AddColumn");
    assert!(add.statements.is_empty() && add.twin);
    assert!(matches!(add.marker, Some(Marker::DataDependent(_))));

    // SQLite takes the drop in place, but not the required column back.
    let revision = plan_revision(&before, &after, Dialect::Sqlite).expect("a revision");
    assert_eq!(kinds(&revision.upgrade), ["DropColumn"]);
    assert_eq!(
        revision.downgrade[0].irreversible.as_deref(),
        Some(
            "AddColumn on card.flavor needs a SQLite table rebuild, which an Alembic revision \
             cannot write; `ferro migrate new` writes it"
        )
    );
}

#[test]
fn an_op_rendering_nothing_is_left_out() {
    let before = live(vec![card(vec![id()])]);
    let after = Side::declared(envelope(vec![card(vec![id(), column("n", "text", true)])]));
    let plan = plan_from_ir(&before, &after, Dialect::Postgres, PlanOptions::default());
    let planned = &plan.operations[0];
    // A rendering with no statement and a report that does not block (row
    // security SQLite skips): the op has nothing to run on this dialect.
    let mut guarded = card(vec![id()]);
    guarded.row_security = Some(SchemaRowSecurity {
        force: false,
        policies: vec![owner_policy()],
    });
    let report = ferro_ddl_lowering::row_security_existing_table_warning(&guarded, Dialect::Sqlite)
        .expect("a skip report");
    assert!(!report.blocks());
    let skipped = RenderedOp {
        op: planned.op.clone(),
        statements: Vec::new(),
        reports: vec![report],
        row_security_statements: Vec::new(),
    };
    assert_eq!(upgrade_op(planned, Some(skipped.clone()), &plan), Ok(None));
    assert_eq!(downgrade_op(planned, Some(skipped), &plan), Ok(None));
}

#[test]
fn an_op_whose_rendering_blocks_is_refused_with_the_renderers_reason() {
    // A column check SQLite cannot drop in place with its column.
    let mut checked = card(vec![id(), column("flavor", "text", true)]);
    checked.checks.push(flavor_check());
    let refusal = plan_revision(
        &live_with(vec![checked], &[("card", flavor_check_fact())], &[]),
        &envelope(vec![card(vec![id()])]),
        Dialect::Sqlite,
    )
    .expect_err("a blocking rendering is refused");
    let RevisionRefusal::Blocked(report) = &refusal else {
        panic!("{refusal:?}");
    };
    assert!(report.blocks());
    assert_eq!(report.subject, Subject::table("card"));
    assert_eq!(refusal.to_string(), report.text);
    assert!(
        refusal
            .to_string()
            .ends_with("`ferro migrate new` to drop it.")
    );
}

#[test]
fn a_refused_rename_hint_refuses_the_revision_by_its_kind() {
    let mut author = table("author", vec![id()]);
    author.renamed_from = Some("writer".into());
    let mut poet = table("poet", vec![id()]);
    poet.renamed_from = Some("writer".into());
    let refusal = plan_revision(
        &live(vec![table("writer", vec![id()])]),
        &envelope(vec![author, poet]),
        Dialect::Postgres,
    )
    .expect_err("refused");
    assert!(
        matches!(&refusal, RevisionRefusal::HintRefused(report)
            if matches!(report.kind, ReportKind::HintRefused(_))),
        "{refusal:?}"
    );
}

#[test]
fn a_primary_key_change_is_refused_with_its_recipe() {
    let mut keyed = card(vec![id(), column("code", "text", false)]);
    keyed.columns[0].primary_key = false;
    keyed.columns[1].primary_key = true;
    let refusal = plan_revision(
        &live(vec![card(vec![id(), column("code", "text", false)])]),
        &envelope(vec![keyed]),
        Dialect::Postgres,
    )
    .expect_err("refused");
    assert!(
        matches!(refusal, RevisionRefusal::Refused(_)),
        "{refusal:?}"
    );
    assert!(
        refusal
            .to_string()
            .starts_with("changing the primary key of \"card\"")
    );
}

#[test]
fn a_label_removal_going_up_is_refused_as_a_ferro_bug() {
    // Only two declared snapshots plan a removal; from a live database the
    // planner never does, so meeting one is refused loudly, before
    // anything renders.
    let removal = PlannedOp {
        op: MigrationOp::RemoveEnumLabel {
            type_name: "rmlorderstatus".into(),
            label: "canceled".into(),
            columns: Vec::new(),
        },
        verdict: crate::plan::OpVerdict::default(),
    };
    let refusal = upgrade_refusal(&removal, Dialect::Postgres).expect("refused");
    assert_eq!(
        refusal,
        RevisionRefusal::SnapshotOnly {
            kind: "RemoveEnumLabel".into()
        }
    );
    assert_eq!(
        refusal.to_string(),
        "the plan carries a RemoveEnumLabel op, which only two declared snapshots plan \
         (`ferro migrate new`), never the live database the Alembic bridge diffs; this is a \
         ferro bug, please file an issue"
    );
    assert_eq!(
        serde_json::to_value(&refusal).expect("serialises")["kind"],
        "snapshot_only"
    );
}

#[test]
fn an_empty_upgrade_is_an_empty_revision() {
    let models = vec![card(vec![id(), column("flavor", "text", true)])];
    let revision =
        plan_revision(&live(models.clone()), &envelope(models), Dialect::Postgres).expect("ok");
    assert_eq!(revision, Revision::default());
}

// -- the downgrade's irreversible reasons (ADR-0050) -----------------------------------

#[test]
fn a_label_the_upgrade_adds_cannot_come_off_a_live_type() {
    let revision = plan_revision(
        &live_with(
            vec![card(vec![
                id(),
                enum_column("status", "status", &["a"], true),
            ])],
            &[],
            &[("status", &["a"])],
        ),
        &envelope(vec![card(vec![
            id(),
            enum_column("status", "status", &["a", "y"], false),
        ])]),
        Dialect::Postgres,
    )
    .expect("a revision");
    assert_eq!(kinds(&revision.upgrade), ["AddEnumLabel"]);
    // The pass's statement, committed before any later statement uses the
    // label.
    assert!(!revision.upgrade[0].twin);
    assert!(revision.upgrade[0].autocommit);
    assert_eq!(kinds(&revision.downgrade), ["RemoveEnumLabel"]);
    assert_eq!(
        revision.downgrade[0].irreversible.as_deref(),
        Some(
            "label \"y\" of enum type status cannot be removed: enum labels are append-only \
             (ADR-0011), and rows may hold it"
        )
    );
}

fn owner_policy() -> SchemaRowPolicy {
    SchemaRowPolicy {
        name: "rls_card_owner".into(),
        command: RowPolicyCommand::Select,
        restrictive: false,
        expr: RowPolicyExpr::Raw {
            using: Some("id > 0".into()),
            with_check: None,
        },
    }
}

#[test]
fn a_policy_applying_to_a_role_list_cannot_be_put_back() {
    let mut guarded = card(vec![id()]);
    guarded.row_security = Some(SchemaRowSecurity {
        force: false,
        policies: vec![owner_policy()],
    });
    let facts = LiveTableFacts {
        row_security: LiveRowSecurity {
            enabled: true,
            forced: false,
            policies: vec![LiveRowPolicy {
                name: "rls_card_owner".into(),
                command: "select".into(),
                restrictive: false,
                using: Some("(id > 0)".into()),
                with_check: None,
                roles: vec!["app".into()],
                ferro_owned: true,
            }],
        },
        ..LiveTableFacts::default()
    };
    let revision = plan_revision(
        &live_with(vec![guarded], &[("card", facts)], &[]),
        &envelope(vec![card(vec![id()])]),
        Dialect::Postgres,
    )
    .expect("a revision");
    let restore = revision
        .downgrade
        .iter()
        .find(|op| matches!(op.op, MigrationOp::AddRowPolicy { .. }))
        .expect("the policy's restore");
    assert_eq!(
        restore.irreversible.as_deref(),
        Some(
            "row policy rls_card_owner on card applies TO app, a clause ferro's CREATE POLICY \
             never writes; restore it by hand"
        )
    );
}

fn linked_card(fk: bool) -> SchemaModel {
    let mut model = card(vec![id(), column("owner_id", "int", true)]);
    if fk {
        model.foreign_keys.push(SchemaForeignKey {
            column: "owner_id".into(),
            to_table: "team".into(),
            to_column: "id".into(),
            on_delete: None,
            name: Some("fk_card_owner_id_team".into()),
            renamed_from: None,
        });
    }
    model
}

#[test]
fn what_sqlite_cannot_undo_in_place_is_irreversible() {
    // The upgrade adds a column with its foreign key, which SQLite writes
    // inline in `ADD COLUMN`; the column comes off only by a rebuild.
    let team = table("team", vec![id()]);
    let revision = plan_revision(
        &live(vec![team.clone(), card(vec![id()])]),
        &envelope(vec![team, linked_card(true)]),
        Dialect::Sqlite,
    )
    .expect("a revision");
    assert_eq!(kinds(&revision.upgrade), ["AddColumn"]);
    // SQLite's inline `REFERENCES` runs as the pass writes it.
    assert!(!revision.upgrade[0].twin);
    assert_eq!(
        revision
            .downgrade
            .iter()
            .map(|op| (kind_name(&op.op), op.irreversible.as_deref()))
            .collect::<Vec<_>>(),
        [(
            "DropColumn".to_string(),
            Some(
                "DropColumn on card.owner_id needs a SQLite table rebuild, which an Alembic \
                 revision cannot write; `ferro migrate new` writes it"
            )
        )]
    );

    // A column check: the column's drop takes it, but SQLite cannot drop
    // it in place; the renderer's reason is the irreversible one.
    let mut checked = card(vec![id(), column("flavor", "text", true)]);
    checked.checks.push(flavor_check());
    let revision = plan_revision(
        &live(vec![card(vec![id()])]),
        &envelope(vec![checked]),
        Dialect::Sqlite,
    )
    .expect("a revision");
    assert_eq!(kinds(&revision.upgrade), ["AddColumn"]);
    assert_eq!(kinds(&revision.downgrade), ["DropCheck", "DropColumn"]);
    let reason = revision.downgrade[0]
        .irreversible
        .as_deref()
        .unwrap_or_default();
    assert!(
        reason
            .starts_with("CHECK constraint 'ck_card_flavor' on table 'card' is no longer declared"),
        "{reason}"
    );
    assert_eq!(revision.downgrade[1].irreversible, None);
}

// -- ADR-0041's amendment: a dropped model's table ---------------------------------------

#[test]
fn a_dropped_table_goes_before_its_type_and_comes_back_after_it() {
    // The live IR carries no check and no policy: those are the facts'.
    let tag = table(
        "tag",
        vec![id(), enum_column("kind", "tagkind", &["a", "b"], true)],
    );
    let facts = LiveTableFacts {
        checks: vec![LiveCheckFact {
            name: "ck_tag_positive".into(),
            definition: "CHECK ((id IS NOT NULL))".into(),
            ferro_owned: true,
            validated: true,
        }],
        row_security: LiveRowSecurity {
            enabled: true,
            forced: false,
            policies: vec![LiveRowPolicy {
                name: "rls_tag_owner".into(),
                command: "select".into(),
                restrictive: false,
                using: Some("(id > 0)".into()),
                with_check: None,
                roles: vec!["public".into()],
                ferro_owned: true,
            }],
        },
        ..LiveTableFacts::default()
    };
    let revision = plan_revision(
        &live_with(vec![tag], &[("tag", facts)], &[("tagkind", &["a", "b"])]),
        &envelope(Vec::new()),
        Dialect::Postgres,
    )
    .expect("a revision");
    assert_eq!(kinds(&revision.upgrade), ["DropTable", "DropEnumType"]);
    assert_eq!(
        revision
            .upgrade
            .iter()
            .map(|op| op.marker.as_ref().map(Marker::comment))
            .collect::<Vec<_>>(),
        [
            Some("destructive (drops tag and the data it holds)"),
            Some("destructive (drops tagkind and the data it holds)"),
        ]
    );
    // The type, then the table, its check and its policy as the catalog
    // printed them, and its row security.
    assert_eq!(
        kinds(&revision.downgrade),
        [
            "CreateEnumType",
            "AddTable",
            "AddCheck",
            "EnableRowSecurity",
            "AddRowPolicy"
        ]
    );
    assert!(revision.downgrade[1].twin);
    assert_eq!(
        revision.downgrade[2].statements,
        ["ALTER TABLE \"tag\" ADD CONSTRAINT \"ck_tag_positive\" CHECK ((id IS NOT NULL))"]
    );
    assert!(
        revision.downgrade[4].statements[0].starts_with("CREATE POLICY \"rls_tag_owner\""),
        "{:?}",
        revision.downgrade[4].statements
    );
}

// -- ADR-0051: a redefined index and a removed foreign key -----------------------------

#[test]
fn a_redefined_index_carries_the_definition_each_side_leads_to() {
    let indexed = |columns: &[&str]| {
        let mut model = card(vec![
            id(),
            column("a", "text", true),
            column("b", "text", true),
        ]);
        model.indexes.push(SchemaIndex {
            name: "idx_card_a".into(),
            columns: columns.iter().map(|c| c.to_string()).collect(),
            unique: false,
        });
        model
    };
    let revision = plan_revision(
        &live(vec![indexed(&["a"])]),
        &envelope(vec![indexed(&["a", "b"])]),
        Dialect::Postgres,
    )
    .expect("a revision");
    let [up] = revision.upgrade.as_slice() else {
        panic!("{:?}", revision.upgrade);
    };
    assert_eq!(kind_name(&up.op), "RedefineIndex");
    assert!(up.twin);
    assert_eq!(up.index, Some((vec!["a".into(), "b".into()], false)));
    let [down] = revision.downgrade.as_slice() else {
        panic!("{:?}", revision.downgrade);
    };
    assert_eq!(down.index, Some((vec!["a".into()], false)));
}

#[test]
fn a_removed_foreign_key_is_alembics_drop_constraint_both_ways() {
    let team = table("team", vec![id()]);
    let revision = plan_revision(
        &live(vec![team.clone(), linked_card(true)]),
        &envelope(vec![team, linked_card(false)]),
        Dialect::Postgres,
    )
    .expect("a revision");
    assert_eq!(kinds(&revision.upgrade), ["DropForeignKey"]);
    assert!(revision.upgrade[0].twin);
    assert_eq!(kinds(&revision.downgrade), ["AddForeignKey"]);
    assert!(revision.downgrade[0].twin);
    // The key put back is the live one, as `create_foreign_key` takes it.
    assert_eq!(revision.downgrade[0].foreign_key, Some(team_key()));
}

fn team_key() -> RevisionForeignKey {
    RevisionForeignKey {
        name: Some("fk_card_owner_id_team".into()),
        column: "owner_id".into(),
        to_table: "team".into(),
        to_column: "id".into(),
        on_delete: None,
    }
}

#[test]
fn a_demanding_column_carries_its_foreign_key_rider() {
    // A required `owner_id` with its foreign key, added to a table with
    // rows: the pass has no statement, so the revision writes Alembic's
    // plain add and, beside it, the key that rides the column.
    let team = table("team", vec![id()]);
    let mut required = linked_card(true);
    required.columns[1].nullable = false;
    let revision = plan_revision(
        &live(vec![team.clone(), card(vec![id()])]),
        &envelope(vec![team, required]),
        Dialect::Postgres,
    )
    .expect("a revision");
    let [add] = revision.upgrade.as_slice() else {
        panic!("{:?}", revision.upgrade);
    };
    assert_eq!(kind_name(&add.op), "AddColumn");
    assert!(add.statements.is_empty() && add.twin);
    assert!(matches!(add.marker, Some(Marker::DataDependent(_))));
    assert_eq!(add.foreign_key, Some(team_key()));

    // A nullable one is the pass's own statement, its key inside it.
    let revision = plan_revision(
        &live(vec![table("team", vec![id()]), card(vec![id()])]),
        &envelope(vec![table("team", vec![id()]), linked_card(true)]),
        Dialect::Postgres,
    )
    .expect("a revision");
    assert!(revision.upgrade.iter().all(|op| op.foreign_key.is_none()));
}

// -- the wire shape ---------------------------------------------------------------------

#[test]
fn a_revision_op_serialises_its_marker_with_its_comment() {
    let before = live(vec![card(vec![id()])]);
    let after = envelope(vec![card(vec![id(), column("flavor", "text", false)])]);
    let revision = plan_revision(&before, &after, Dialect::Postgres).expect("a revision");
    let wire = serde_json::to_value(&revision).expect("serialises");
    assert_eq!(
        wire["upgrade"][0],
        serde_json::json!({
            "op": {"kind": "AddColumn", "table": "card", "column": "flavor"},
            "statements": [],
            "row_security_statements": [],
            "twin": true,
            "autocommit": false,
            "foreign_key": null,
            "index": null,
            "marker": {
                "kind": "data_dependent",
                "comment": "data-dependent (fails while card has rows; ferro migrations \
                            generate the backfill: `ferro migrate new`)",
            },
            "irreversible": null,
        })
    );
    let refusal = plan_revision(&before, &after, Dialect::Sqlite).expect_err("refused");
    let wire = serde_json::to_value(&refusal).expect("serialises");
    assert_eq!(wire["kind"], "sqlite_required_column");
    assert_eq!(wire["text"], refusal.to_string());
}
