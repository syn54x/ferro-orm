//! The phase table: which phase step each op of a migration's up lands in,
//! read off the op's verdict ([`crate::OpVerdict`]) and whether the migration
//! has a data step — nothing else (ADR-0050).
//!
//! ```text
//! class Author(Model):              ferro migrate new author_bio
//!     bio: str | None = None   ──▶  AddColumn  author.bio  → schema
//!     name: str                ──▶  AddColumn  author.name → demands values: expand, backfill, contract
//!     age: str  # was int      ──▶  AlterColumnType on SQLite → schema, a table rebuild
//! ```
//!
//! The planner decides what is true of each op once (its execution on the
//! dialect, whether it demands values of existing rows, drops data, or rides
//! a dropped column's statement); this table only decides where the
//! generator writes it. Every statement still comes from the plan's renderer
//! or [`super::rebuild::render`] (AGENTS.md § I-1).

use crate::{Dialect, Execution, MigrationOp, PlannedOp, Rider};

/// The phase step an op lands in, in the order a migration's steps run.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Phase {
    /// Label additions to enum types that already exist (ADR-0011, ADR-0041):
    /// first in the migration, so every later step may write the new label.
    Labels,
    /// The one atomic DDL step of a migration that needs no data step.
    Schema,
    /// Columns added nullable ahead of a backfill ([`super::backfill`]).
    Expand,
    /// A data step filling values existing rows lack.
    Backfill,
    /// One index built or dropped on an existing table (ticket #527).
    Index,
    /// Postgres staged `NOT NULL` checks installed `NOT VALID`.
    AddConstraint,
    /// Validation and `SET NOT NULL` after a backfill, and every op of a
    /// migration with a data step that drops data or goes with a dropped
    /// column: the data steps still read what it removes (ADR-0025).
    Contract,
    /// Validation of staged constraints with no contract (ticket #527).
    Validate,
}

impl Phase {
    /// The step's name after `NN_`.
    pub fn step_name(self) -> &'static str {
        match self {
            Phase::Labels => "labels",
            Phase::Schema => "schema",
            Phase::Expand => "expand",
            Phase::Backfill => "backfill",
            Phase::Index => "index",
            Phase::AddConstraint => "add_constraint",
            Phase::Contract => "contract",
            Phase::Validate => "validate",
        }
    }
}

/// The phase step `op` lands in, in a migration that has (`data_steps`) or
/// lacks a data step after its schema step, or `None` for an op no door
/// runs ([`Execution::Refused`], [`Execution::Irreversible`]), whose verdict
/// the caller turns into its refusal.
///
/// | The op's verdict | Phase |
/// | :-- | :-- |
/// | an enum label added | labels |
/// | demands values of existing rows | backfill (the migration's expand → backfill → contract) |
/// | drops data, or goes with a dropped column, beside a data step | contract |
/// | an index built, dropped or redefined on its own | index |
/// | anything else, natively or by a SQLite rebuild | schema |
pub(crate) fn phase(op: &PlannedOp, data_steps: bool) -> Option<Phase> {
    let verdict = &op.verdict;
    if matches!(
        verdict.execution,
        Execution::Refused(_) | Execution::Irreversible(_)
    ) {
        return None;
    }
    Some(if matches!(op.op, MigrationOp::AddEnumLabel { .. }) {
        Phase::Labels
    } else if verdict.demands_values {
        Phase::Backfill
    } else if data_steps && (verdict.drops_data || verdict.goes_with == Some(Rider::DroppedColumn))
    {
        Phase::Contract
    } else if matches!(
        op.op,
        MigrationOp::AddIndex { .. }
            | MigrationOp::DropIndex { .. }
            | MigrationOp::RedefineIndex { .. }
    ) && verdict.goes_with.is_none()
    {
        Phase::Index
    } else {
        Phase::Schema
    })
}

/// Whether a file on `dialect` leaves `op` out: on SQLite, the drop of a
/// dropped column's own inline check, which its `DROP COLUMN` already removes
/// (the renderer has no statement for it, only a report that would be false
/// here).
pub(crate) fn omitted(op: &PlannedOp, dialect: Dialect) -> bool {
    dialect == Dialect::Sqlite
        && matches!(op.op, MigrationOp::DropCheck { .. })
        && op.verdict.goes_with == Some(Rider::DroppedColumn)
}

#[cfg(test)]
mod tests {
    //! The verdict table, `(op, sides, dialect) → OpVerdict`, and the up
    //! reading this phase table gives it. The down's readings are asserted
    //! by its consumers ([`super::super::downs`]).
    use super::super::tests::{column, ir, model, pk};
    use super::*;
    use crate::{OpVerdict, Refusal, RowRisk, Side};
    use ferro_schema_ir::{SchemaCheck, SchemaColumn, SchemaForeignKey, SchemaIndex, SchemaModel};

    const DIALECTS: [Dialect; 2] = [Dialect::Postgres, Dialect::Sqlite];

    fn nullable(name: &str, logical_type: &str) -> SchemaColumn {
        SchemaColumn {
            nullable: true,
            ..column(name, logical_type)
        }
    }

    fn defaulted(name: &str) -> SchemaColumn {
        SchemaColumn {
            default: Some(serde_json::json!("free")),
            ..column(name, "string")
        }
    }

    fn author(extra: Vec<SchemaColumn>) -> SchemaModel {
        let mut columns = vec![pk(), column("name", "string")];
        columns.extend(extra);
        model("Author", columns)
    }

    fn with_fk(mut model: SchemaModel, col: &str) -> SchemaModel {
        model.foreign_keys.push(SchemaForeignKey {
            renamed_from: None,
            column: col.into(),
            to_table: "team".into(),
            to_column: "id".into(),
            on_delete: Some("CASCADE".into()),
            name: Some(format!("fk_author_{col}_team")),
        });
        model
    }

    /// The verdict of `op` planned from the models `before` to `after`.
    fn verdict_between(
        op: &MigrationOp,
        before: Vec<SchemaModel>,
        after: Vec<SchemaModel>,
        dialect: Dialect,
    ) -> PlannedOp {
        PlannedOp::of(
            op.clone(),
            &Side::declared(ir(before)),
            &Side::declared(ir(after)),
            dialect,
        )
    }

    fn verdict(
        op: &MigrationOp,
        before: &SchemaModel,
        after: &SchemaModel,
        dialect: Dialect,
    ) -> OpVerdict {
        verdict_between(op, vec![before.clone()], vec![after.clone()], dialect).verdict
    }

    fn execution(
        op: &MigrationOp,
        before: &SchemaModel,
        after: &SchemaModel,
        dialect: Dialect,
    ) -> Execution {
        verdict(op, before, after, dialect).execution
    }

    fn add(column: &str) -> MigrationOp {
        MigrationOp::AddColumn {
            table: "author".into(),
            column: column.into(),
        }
    }

    fn drop(column: &str) -> MigrationOp {
        MigrationOp::DropColumn {
            table: "author".into(),
            column: column.into(),
        }
    }

    #[test]
    fn the_verdict_reads_the_ops_table_on_both_sides() {
        // A table only the target holds: an added column of it asks no
        // existing row for anything; a table on both sides does.
        let after = author(vec![column("slug", "string")]);
        let created = verdict_between(&add("slug"), vec![], vec![after.clone()], Dialect::Postgres);
        assert!(!created.verdict.demands_values);
        let existing = verdict(&add("slug"), &author(vec![]), &after, Dialect::Postgres);
        assert!(existing.demands_values);
        // An enum type's op names no table: nothing about a table is true.
        let enum_op = MigrationOp::CreateEnumType {
            type_name: "status".into(),
            labels: vec![],
        };
        assert_eq!(
            verdict(&enum_op, &after, &after, Dialect::Postgres),
            OpVerdict::default()
        );
    }

    #[test]
    fn whole_tables_and_enum_types_are_native_everywhere() {
        let ops = [
            MigrationOp::AddTable {
                table: "author".into(),
            },
            MigrationOp::DropTable {
                table: "author".into(),
            },
            MigrationOp::CreateEnumType {
                type_name: "status".into(),
                labels: vec!["a".into()],
            },
            MigrationOp::DropEnumType {
                type_name: "status".into(),
            },
        ];
        for op in &ops {
            for dialect in DIALECTS {
                let v = verdict(op, &author(vec![]), &author(vec![]), dialect);
                assert_eq!(v.execution, Execution::Native, "{op:?} {dialect:?}");
            }
        }
        // A table and a type dropped drop data; a table created brings back
        // one whose rows are gone (read by a down).
        let a = author(vec![]);
        assert!(verdict(&ops[1], &a, &a, Dialect::Postgres).drops_data);
        assert!(verdict(&ops[3], &a, &a, Dialect::Postgres).drops_data);
        assert!(verdict(&ops[0], &a, &a, Dialect::Postgres).recreates);
    }

    #[test]
    fn adding_a_column_by_shape_and_dialect() {
        let before = author(vec![]);
        // (column, foreign key, dialect) → (execution, demands values)
        let cases: Vec<(SchemaColumn, bool, Dialect, Execution, bool)> = vec![
            // A1: an optional column.
            (
                nullable("bio", "string"),
                false,
                Dialect::Postgres,
                Execution::Native,
                false,
            ),
            (
                nullable("bio", "string"),
                false,
                Dialect::Sqlite,
                Execution::Native,
                false,
            ),
            // A1 with a foreign key: SQLite takes an inline REFERENCES.
            (
                nullable("team_id", "integer"),
                true,
                Dialect::Sqlite,
                Execution::Native,
                false,
            ),
            (
                nullable("team_id", "integer"),
                true,
                Dialect::Postgres,
                Execution::Native,
                false,
            ),
            // A2: a literal default fills existing rows.
            (
                defaulted("tier"),
                false,
                Dialect::Postgres,
                Execution::Native,
                false,
            ),
            (
                defaulted("tier"),
                false,
                Dialect::Sqlite,
                Execution::Native,
                false,
            ),
            // A required FK column with a default: no REFERENCES on SQLite.
            (
                SchemaColumn {
                    default: Some(serde_json::json!(1)),
                    ..column("team_id", "integer")
                },
                true,
                Dialect::Sqlite,
                Execution::Rebuild,
                false,
            ),
            // A3: required, no default — it demands values everywhere; SQLite
            // has no `SET NOT NULL`, so taking it as it is needs a rebuild.
            (
                column("slug", "string"),
                false,
                Dialect::Postgres,
                Execution::Native,
                true,
            ),
            (
                column("slug", "string"),
                false,
                Dialect::Sqlite,
                Execution::Rebuild,
                true,
            ),
            // A null default is no default.
            (
                SchemaColumn {
                    default: Some(serde_json::Value::Null),
                    ..column("slug", "string")
                },
                false,
                Dialect::Postgres,
                Execution::Native,
                true,
            ),
            // A primary key added to an existing table.
            (
                SchemaColumn {
                    primary_key: true,
                    ..column("uid", "string")
                },
                false,
                Dialect::Postgres,
                Execution::Refused(Refusal::PrimaryKeyChange {
                    table: "author".into(),
                }),
                false,
            ),
        ];
        for (col, fk, dialect, expected, demands) in cases {
            let name = col.name.clone();
            let mut after = author(vec![col]);
            if fk {
                after = with_fk(after, &name);
            }
            let v = verdict(&add(&name), &before, &after, dialect);
            assert_eq!(v.execution, expected, "{name} {dialect:?}");
            assert_eq!(v.demands_values, demands, "{name} {dialect:?}");
        }
        // The up's reading: a demanding column is the migration's backfill.
        let after = author(vec![column("slug", "string")]);
        for dialect in DIALECTS {
            let planned = verdict_between(
                &add("slug"),
                vec![before.clone()],
                vec![after.clone()],
                dialect,
            );
            assert_eq!(phase(&planned, false), Some(Phase::Backfill));
            // A NOT NULL column comes back with no rows' values.
            assert!(planned.verdict.recreates);
            assert_eq!(planned.verdict.fails_on_rows, RowRisk::Always);
        }
    }

    #[test]
    fn dropping_a_column_by_shape_and_dialect() {
        let after = author(vec![]);
        for dialect in DIALECTS {
            // A4: a plain column, nullable or not.
            for col in [nullable("bio", "string"), column("bio", "string")] {
                let v = verdict(&drop("bio"), &author(vec![col]), &after, dialect);
                assert_eq!(v.execution, Execution::Native);
                assert!(v.drops_data);
            }
            assert_eq!(
                execution(
                    &drop("id"),
                    &author(vec![]),
                    &model("Author", vec![column("name", "string")]),
                    dialect,
                ),
                Execution::Refused(Refusal::PrimaryKeyChange {
                    table: "author".into()
                })
            );
        }
        // C2: an FK column. SQLite cannot drop a table-level FOREIGN KEY's
        // column in place, the shape every rebuild writes.
        let before = with_fk(author(vec![nullable("team_id", "integer")]), "team_id");
        assert_eq!(
            execution(&drop("team_id"), &before, &after, Dialect::Sqlite),
            Execution::Rebuild
        );
        assert_eq!(
            execution(&drop("team_id"), &before, &after, Dialect::Postgres),
            Execution::Native
        );
    }

    #[test]
    fn a_drop_waits_for_the_contract_only_in_a_migration_with_a_data_step() {
        let mut with_bio = author(vec![SchemaColumn {
            index: true,
            ..nullable("bio", "string")
        }]);
        with_bio.checks.push(SchemaCheck {
            name: "ck_author_bio".into(),
            column: "bio".into(),
            values: vec!["'a'".into()],
        });
        with_bio.indexes.push(SchemaIndex {
            name: "idx_author_bio".into(),
            columns: vec!["bio".into()],
            unique: false,
        });
        let without = author(vec![]);
        let drop_index = MigrationOp::DropIndex {
            table: "author".into(),
            name: "idx_author_bio".into(),
        };
        let drop_check = MigrationOp::DropCheck {
            table: "author".into(),
            name: "ck_author_bio".into(),
        };
        // Each a drop of data or what goes with a dropped column.
        let cases = [
            (drop("bio"), vec![with_bio.clone()], vec![without.clone()]),
            (drop_index, vec![with_bio.clone()], vec![without.clone()]),
            (drop_check, vec![with_bio.clone()], vec![without.clone()]),
            (
                MigrationOp::DropTable {
                    table: "author".into(),
                },
                vec![with_bio.clone()],
                vec![],
            ),
            (
                MigrationOp::DropEnumType {
                    type_name: "status".into(),
                },
                vec![with_bio.clone()],
                vec![],
            ),
        ];
        for (op, before, after) in &cases {
            for dialect in DIALECTS {
                let planned = verdict_between(op, before.clone(), after.clone(), dialect);
                assert_ne!(phase(&planned, false), Some(Phase::Contract), "{op:?}");
                assert_eq!(
                    phase(&planned, true),
                    Some(Phase::Contract),
                    "{op:?} {dialect:?}"
                );
            }
        }
        // What discards no data stays where it is: a check dropped from a
        // column that stays, a relaxed NOT NULL, a new column or table.
        let unchecked = author(vec![nullable("bio", "string")]);
        let mut checked = unchecked.clone();
        checked.checks.push(SchemaCheck {
            name: "ck_author_bio".into(),
            column: "bio".into(),
            values: vec!["'a'".into()],
        });
        let stays = [
            (
                MigrationOp::DropCheck {
                    table: "author".into(),
                    name: "ck_author_bio".into(),
                },
                vec![checked],
                vec![unchecked],
            ),
            (add("bio"), vec![without.clone()], vec![with_bio.clone()]),
            (
                MigrationOp::AddTable {
                    table: "author".into(),
                },
                vec![],
                vec![with_bio.clone()],
            ),
            (
                MigrationOp::AlterColumnNullability {
                    table: "author".into(),
                    column: "name".into(),
                },
                vec![author(vec![])],
                vec![model("Author", vec![pk(), nullable("name", "string")])],
            ),
        ];
        for (op, before, after) in &stays {
            let planned = verdict_between(op, before.clone(), after.clone(), Dialect::Postgres);
            assert_ne!(phase(&planned, true), Some(Phase::Contract), "{op:?}");
        }
    }

    #[test]
    fn a_type_change_is_native_on_postgres_and_a_rebuild_on_sqlite() {
        let op = MigrationOp::AlterColumnType {
            table: "author".into(),
            column: "age".into(),
        };
        let before = author(vec![column("age", "integer")]);
        let after = author(vec![column("age", "string")]);
        assert_eq!(
            execution(&op, &before, &after, Dialect::Postgres),
            Execution::Native
        );
        assert_eq!(
            execution(&op, &after, &before, Dialect::Postgres),
            Execution::Native
        );
        assert_eq!(
            execution(&op, &before, &after, Dialect::Sqlite),
            Execution::Rebuild
        );
        assert_eq!(
            execution(&op, &after, &before, Dialect::Sqlite),
            Execution::Rebuild
        );
        assert_eq!(
            verdict(&op, &before, &after, Dialect::Postgres).fails_on_rows,
            RowRisk::Always
        );
        // To or from a native enum type: refused with the recipe.
        let status = SchemaColumn {
            enum_values: Some(vec![serde_json::json!("a")]),
            enum_type_name: Some("status".into()),
            ..column("age", "string")
        };
        assert_eq!(
            execution(&op, &before, &author(vec![status]), Dialect::Postgres),
            Execution::Refused(Refusal::EnumTypeMove {
                table: "author".into(),
                column: "age".into()
            })
        );
        // The primary key's type.
        let id_op = MigrationOp::AlterColumnType {
            table: "author".into(),
            column: "id".into(),
        };
        let uuid_pk = model(
            "Author",
            vec![
                SchemaColumn {
                    primary_key: true,
                    ..column("id", "uuid")
                },
                column("name", "string"),
            ],
        );
        for dialect in DIALECTS {
            let planned =
                verdict_between(&id_op, vec![author(vec![])], vec![uuid_pk.clone()], dialect);
            assert_eq!(
                planned.verdict.execution,
                Execution::Refused(Refusal::PrimaryKeyChange {
                    table: "author".into()
                })
            );
            assert_eq!(phase(&planned, false), None);
        }
    }

    #[test]
    fn relaxing_not_null_is_native_on_postgres_and_requiring_it_demands_values() {
        let op = MigrationOp::AlterColumnNullability {
            table: "author".into(),
            column: "bio".into(),
        };
        let required = author(vec![column("bio", "string")]);
        let optional = author(vec![nullable("bio", "string")]);
        // A7b: relaxed — native on Postgres, nothing asked of the rows.
        let relax = verdict(&op, &required, &optional, Dialect::Postgres);
        assert_eq!(relax.execution, Execution::Native);
        assert!(!relax.demands_values);
        assert_eq!(relax.fails_on_rows, RowRisk::None);
        // A7a: required — values for the NULL rows first (ADR-0042), whichever
        // way a file runs it.
        let require = verdict(&op, &optional, &required, Dialect::Postgres);
        assert_eq!(require.execution, Execution::Native);
        assert!(require.demands_values);
        assert_eq!(require.fails_on_rows, RowRisk::Always);
        // SQLite rebuilds either way.
        for (before, after) in [(&required, &optional), (&optional, &required)] {
            assert_eq!(
                execution(&op, before, after, Dialect::Sqlite),
                Execution::Rebuild
            );
        }
        // The up's reading: a column made required is the backfill on both.
        for dialect in DIALECTS {
            let planned =
                verdict_between(&op, vec![optional.clone()], vec![required.clone()], dialect);
            assert_eq!(phase(&planned, false), Some(Phase::Backfill));
        }
    }

    #[test]
    fn an_index_on_an_existing_table_is_its_own_index_step_on_every_dialect() {
        let index = SchemaIndex {
            name: "idx_author_name_bio".into(),
            columns: vec!["name".into(), "bio".into()],
            unique: false,
        };
        let plain = author(vec![nullable("bio", "string")]);
        let indexed = SchemaModel {
            indexes: vec![index],
            ..plain.clone()
        };
        let add_index = MigrationOp::AddIndex {
            table: "author".into(),
            name: "idx_author_name_bio".into(),
            columns: vec!["name".into(), "bio".into()],
            unique: false,
        };
        let drop_index = MigrationOp::DropIndex {
            table: "author".into(),
            name: "idx_author_name_bio".into(),
        };
        for dialect in DIALECTS {
            for (op, before, after) in [
                (&add_index, &plain, &indexed),
                (&drop_index, &indexed, &plain),
            ] {
                let planned =
                    verdict_between(op, vec![before.clone()], vec![after.clone()], dialect);
                assert_eq!(planned.verdict.execution, Execution::Native);
                assert_eq!(phase(&planned, false), Some(Phase::Index), "{op:?}");
            }
        }
        // An index over a column the same plan adds rides the column (a down
        // putting back a column dropped with its index puts it back with it,
        // in the schema step); one over a column it drops goes with the drop.
        let without_bio = author(vec![]);
        let restore = verdict_between(
            &add_index,
            vec![without_bio.clone()],
            vec![indexed.clone()],
            Dialect::Postgres,
        );
        assert_eq!(restore.verdict.goes_with, Some(Rider::AddedColumn));
        assert_eq!(phase(&restore, false), Some(Phase::Schema));
        let dropped = verdict_between(
            &drop_index,
            vec![indexed.clone()],
            vec![without_bio],
            Dialect::Postgres,
        );
        assert_eq!(dropped.verdict.goes_with, Some(Rider::DroppedColumn));
        assert_eq!(dropped.verdict.execution, Execution::Native);
        assert!(!omitted(&dropped, Dialect::Postgres));
        // A unique index scans the rows.
        let unique = MigrationOp::AddIndex {
            table: "author".into(),
            name: "uq_author_bio".into(),
            columns: vec!["bio".into()],
            unique: true,
        };
        assert_eq!(
            verdict(&unique, &plain, &plain, Dialect::Postgres).fails_on_rows,
            RowRisk::Always
        );
    }

    #[test]
    fn a_dropped_columns_own_check_goes_with_it_and_any_other_check_change_is_native_on_postgres() {
        let check = SchemaCheck {
            name: "ck_author_tier".into(),
            column: "tier".into(),
            values: vec!["'free'".into()],
        };
        let checked = SchemaModel {
            checks: vec![check],
            ..author(vec![nullable("tier", "string")])
        };
        let drop_check = MigrationOp::DropCheck {
            table: "author".into(),
            name: "ck_author_tier".into(),
        };
        for dialect in DIALECTS {
            let planned = verdict_between(
                &drop_check,
                vec![checked.clone()],
                vec![author(vec![])],
                dialect,
            );
            assert_eq!(planned.verdict.execution, Execution::Native);
            assert_eq!(planned.verdict.goes_with, Some(Rider::DroppedColumn));
            assert_eq!(omitted(&planned, dialect), dialect == Dialect::Sqlite);
        }
        // The column stays and only its check goes: an A10 drop.
        let unchecked = author(vec![nullable("tier", "string")]);
        assert_eq!(
            execution(&drop_check, &checked, &unchecked, Dialect::Sqlite),
            Execution::Rebuild
        );
        assert_eq!(
            execution(&drop_check, &checked, &unchecked, Dialect::Postgres),
            Execution::Native
        );
        let staged = [
            MigrationOp::AddCheck {
                table: "author".into(),
                name: "ck_author_tier".into(),
            },
            MigrationOp::RebuildCheck {
                table: "author".into(),
                name: "ck_author_tier".into(),
            },
            MigrationOp::AddForeignKey {
                table: "author".into(),
                column: "tier".into(),
            },
            MigrationOp::RebuildForeignKey {
                table: "author".into(),
                column: "tier".into(),
                old_name: "fk".into(),
            },
        ];
        for op in &staged {
            assert_eq!(
                execution(op, &checked, &checked, Dialect::Sqlite),
                Execution::Rebuild
            );
            let v = verdict(op, &checked, &checked, Dialect::Postgres);
            assert_eq!(v.execution, Execution::Native);
            // Validated as it is added, it scans the rows; staged NOT VALID
            // (the reader's choice), it scans nothing.
            assert_eq!(v.fails_on_rows, RowRisk::WhenValidated);
        }
    }

    #[test]
    fn enum_label_and_type_ops_have_their_steps_and_are_native_everywhere() {
        let a = author(vec![]);
        let label = MigrationOp::AddEnumLabel {
            type_name: "status".into(),
            label: "x".into(),
        };
        let renames = [
            MigrationOp::RenameEnumLabel {
                type_name: "status".into(),
                old: "x".into(),
                new: "y".into(),
                columns: vec![("author".into(), "status".into())],
            },
            MigrationOp::RenameEnumType {
                old: "status".into(),
                new: "state".into(),
            },
        ];
        for dialect in DIALECTS {
            let planned = verdict_between(&label, vec![a.clone()], vec![a.clone()], dialect);
            assert_eq!(planned.verdict.execution, Execution::Native);
            assert_eq!(phase(&planned, false), Some(Phase::Labels));
            for op in &renames {
                let planned = verdict_between(op, vec![a.clone()], vec![a.clone()], dialect);
                assert_eq!(planned.verdict.execution, Execution::Native, "{op:?}");
                assert_eq!(phase(&planned, false), Some(Phase::Schema), "{op:?}");
            }
        }
        // A removed label asks the rows holding it for a value (D2): the
        // migration's backfill.
        let removal = MigrationOp::RemoveEnumLabel {
            type_name: "status".into(),
            label: "x".into(),
            columns: vec![],
        };
        let planned = verdict_between(&removal, vec![a.clone()], vec![a], Dialect::Postgres);
        assert!(planned.verdict.demands_values);
        assert_eq!(phase(&planned, false), Some(Phase::Backfill));
        // The labels step runs first of every phase step.
        assert!(Phase::Labels < Phase::Schema);
        assert_eq!(Phase::Labels.step_name(), "labels");
    }

    #[test]
    fn row_security_ops_are_native_in_the_schema_step() {
        let a = author(vec![]);
        let table = || "author".to_string();
        let ops = [
            MigrationOp::AddRowPolicy {
                table: table(),
                name: "p".into(),
            },
            MigrationOp::RebuildRowPolicy {
                table: table(),
                name: "p".into(),
            },
            MigrationOp::DropRowPolicy {
                table: table(),
                name: "p".into(),
            },
            MigrationOp::EnableRowSecurity { table: table() },
            MigrationOp::ForceRowSecurity { table: table() },
            MigrationOp::DisableRowSecurity { table: table() },
            MigrationOp::NoForceRowSecurity { table: table() },
        ];
        for op in &ops {
            let planned = verdict_between(op, vec![a.clone()], vec![a.clone()], Dialect::Postgres);
            assert_eq!(planned.verdict, OpVerdict::default(), "{op:?}");
            assert_eq!(phase(&planned, false), Some(Phase::Schema), "{op:?}");
        }
    }

    #[test]
    fn live_only_ops_are_native_and_never_planned_between_two_declarations() {
        // A `NOT VALID` constraint and an invalid index exist only live: a
        // declaration's facts mark every constraint and index valid, so the
        // planner never plans their repair between two declared snapshots.
        let mut checked = author(vec![nullable("bio", "string")]);
        checked.checks.push(SchemaCheck {
            name: "ck_author_bio".into(),
            column: "bio".into(),
            values: vec!["'a'".into()],
        });
        let side = Side::declared(ir(vec![checked]));
        let plan = crate::plan_from_ir(
            &side,
            &side,
            Dialect::Postgres,
            crate::PlanOptions { destructive: true },
        );
        assert!(plan.is_empty());
        let validate = MigrationOp::ValidateConstraint {
            table: "author".into(),
            name: "ck_author_bio".into(),
        };
        let a = author(vec![]);
        assert_eq!(
            verdict(&validate, &a, &a, Dialect::Postgres),
            OpVerdict::default()
        );
    }

    #[test]
    fn a_primary_key_change_is_the_primary_key_refusal_everywhere() {
        let op = MigrationOp::ChangePrimaryKey {
            table: "author".into(),
            from: vec!["id".into()],
            to: vec!["name".into()],
        };
        let a = author(vec![]);
        for dialect in DIALECTS {
            let planned = verdict_between(&op, vec![a.clone()], vec![a.clone()], dialect);
            assert_eq!(
                planned.verdict.execution,
                Execution::Refused(Refusal::PrimaryKeyChange {
                    table: "author".into()
                })
            );
            assert_eq!(phase(&planned, true), None);
        }
    }
}
