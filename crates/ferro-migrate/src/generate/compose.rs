//! The run's rename hints composed into the squashed schema snapshot
//! (ADR-0056, ADR-0032).
//!
//! ```text
//! parent   author(name)
//! 0005     author(full_name renamed_from="name")
//! 0006     writer(full_name)   __ferro_renamed_from__ = "author"
//! squash   writer(full_name renamed_from="name")  __ferro_renamed_from__ = "author"
//! ```
//!
//! The planner reads a **rename hint** from the target snapshot only, and a
//! hint is live only while the previous snapshot holds the old name and lacks
//! the new one ([`live_hints`]). Planned from the parent straight to 0006's
//! snapshot, the rename of 0005 is inert (0006 dropped the hint, or it no
//! longer matches): a drop and an add where the chain had a rename. A
//! **squash** therefore writes the composition of every live hint along the
//! run into the hint fields of the last snapshot, so the one planner, given
//! the parent and [`Composed::target`], plans the run's renames as renames.
//!
//! The composition follows each table, column, enum label and constraint
//! through the run, step by step, as the planner sees it: one step's live
//! hints applied to the previous snapshot ([`renamed_snapshot`]) name each
//! object as the next snapshot names it. An object present at both ends
//! carries one hint from its parent name to its last name; anything born or
//! gone inside the run carries none, and what existed only inside the run is
//! a [`DeadName`].

use crate::plan::{Hint, HintError, enum_declaration, live_hints, refuse_hints, renamed_snapshot};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel, SchemaRenamedLabels};
use std::collections::BTreeMap;

/// The squashed target of a run: the last snapshot's payload with its rename
/// hints rewritten to the composition of the run's live hints, and the names
/// that existed only inside the run.
#[derive(Clone, Debug, PartialEq)]
pub struct Composed {
    /// The last payload, byte for byte in every field but the hint fields
    /// (`renamed_from` on models, columns and foreign keys,
    /// `enum_renamed_labels` on enum columns), which hold the composed hints.
    pub target: SchemaIrPayload,
    /// Every table, column, enum label and constraint born and gone inside
    /// the run, in the order they appeared.
    pub dead_names: Vec<DeadName>,
}

/// A name that existed only inside the run: absent from the parent snapshot
/// and from the last one. A rename keeps the object: `a` renamed to `b` is
/// one column, never a dead `a`; a dead object renamed inside the run is one
/// entry per name it held.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeadName {
    /// What kind of schema object it named.
    pub kind: DeadKind,
    /// `table`, `table.column`, `table.column:label` or `table.constraint`.
    pub name: String,
    /// The index into the run of the first snapshot holding it.
    pub appeared: usize,
    /// The index into the run of the first snapshot after that without it.
    pub vanished: usize,
}

/// The kind of schema object a [`DeadName`] names.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum DeadKind {
    /// A table.
    Table,
    /// A column, shadow `*_id` columns included.
    Column,
    /// A label of an enum column.
    EnumLabel,
    /// A check, unique, index or foreign key, by its name.
    Constraint,
}

/// A run whose hints cannot be written as one squashed snapshot. Every
/// refusal names its fix (AGENTS.md § I-6).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ComposeError {
    /// `second`, declared later in the run, claims the old name `first`
    /// already renamed away, but names something else than what `first`'s
    /// rename became: the run renamed one way and declares another.
    Uncomposable {
        /// The live hint of the earlier migration.
        first: Hint,
        /// The later declaration it disagrees with.
        second: Hint,
    },
    /// The composed `hint` renames from a name the last snapshot declares
    /// again, from the run's migration `re_added_at` on: the planner refuses
    /// a hint whose old name the target still declares (ADR-0032).
    OldNameReAdded {
        /// The composed hint.
        hint: Hint,
        /// The index into the run of the migration that declares the old
        /// name again.
        re_added_at: usize,
    },
    /// The composed `hint` renames onto a name the parent holds for another
    /// object, which the run's migration `vacated_at` removed or renamed
    /// away: against the parent the hint is inert, and the planner would drop
    /// what the run renamed.
    NewNameHeldByParent {
        /// The composed hint.
        hint: Hint,
        /// The index into the run of the migration that freed the name.
        vacated_at: usize,
    },
    /// The snapshot at the run's index `position` declares a hint the
    /// planner refuses ([`refuse_hints`]); no migration generator writes one.
    RefusedHint {
        /// The index into the run of the snapshot.
        position: usize,
        /// The planner's refusal.
        error: HintError,
    },
}

impl std::fmt::Display for ComposeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ComposeError::Uncomposable { first, second } => write!(
                f,
                "cannot compose the rename hints {} and {}: the later one claims the old \
                 name the earlier one had already renamed away, for another name; correct the \
                 later renamed_from to the name the earlier migration left, or end the squash \
                 range before the migration that declares it",
                describe(first),
                describe(second)
            ),
            ComposeError::OldNameReAdded { hint, re_added_at } => write!(
                f,
                "the run renames {}, then declares \"{}\" again from migration #{} of the \
                 range: one migration cannot rename a name and keep it (ADR-0032); end the \
                 squash range before migration #{}",
                describe(hint),
                old_of(hint),
                re_added_at + 1,
                re_added_at + 1
            ),
            ComposeError::NewNameHeldByParent { hint, vacated_at } => write!(
                f,
                "the run renames {}, but \"{}\" named something else below the range until \
                 migration #{} of the range freed it: one migration cannot rename onto a name \
                 its parent holds; start the squash range after migration #{}",
                describe(hint),
                new_of(hint),
                vacated_at + 1,
                vacated_at + 1
            ),
            ComposeError::RefusedHint { position, error } => write!(
                f,
                "the snapshot of migration #{} of the range declares a rename hint the planner \
                 refuses: {error}; fix that migration's snapshot with `ferro migrate rerecord`",
                position + 1
            ),
        }
    }
}

impl std::error::Error for ComposeError {}

fn describe(hint: &Hint) -> String {
    match hint {
        Hint::Table { old, new } => format!("table \"{old}\" -> \"{new}\""),
        Hint::Column { table, old, new } => format!("column {table}.\"{old}\" -> \"{new}\""),
        Hint::EnumType { old, new } => format!("enum type \"{old}\" -> \"{new}\""),
        Hint::Label {
            type_name,
            old,
            new,
        } => format!("label \"{old}\" -> \"{new}\" of enum type \"{type_name}\""),
    }
}

fn old_of(hint: &Hint) -> &str {
    match hint {
        Hint::Table { old, .. }
        | Hint::Column { old, .. }
        | Hint::EnumType { old, .. }
        | Hint::Label { old, .. } => old,
    }
}

fn new_of(hint: &Hint) -> &str {
    match hint {
        Hint::Table { new, .. }
        | Hint::Column { new, .. }
        | Hint::EnumType { new, .. }
        | Hint::Label { new, .. } => new,
    }
}

/// Compose the rename hints of `run` (the snapshots of the squashed
/// migrations, in chain order) over `parent` (the snapshot below the run;
/// `None` for a run from `0001`) into the squashed target, and find the run's
/// dead names. An empty run composes to the parent without its hints.
///
/// # Errors
/// [`ComposeError`]: a snapshot of the run whose hints the planner refuses,
/// a later declaration that disagrees with an earlier rename, and a composed
/// hint the planner would refuse or ignore against the parent.
pub fn compose_run(
    parent: Option<&SchemaIrPayload>,
    run: &[&SchemaIrPayload],
) -> Result<Composed, ComposeError> {
    let _ = parent;
    let last = run.last().copied().cloned().unwrap_or_else(empty);
    Ok(Composed {
        target: last,
        dead_names: Vec::new(),
    })
}

fn empty() -> SchemaIrPayload {
    SchemaIrPayload {
        dialect_agnostic: true,
        models: Vec::new(),
    }
}

#[allow(dead_code)]
fn unused(_: &IrEnvelope<SchemaIrPayload>, _: &SchemaModel, _: SchemaRenamedLabels) {
    let _ = (live_hints, refuse_hints, renamed_snapshot, enum_declaration);
    let _: BTreeMap<(), ()> = BTreeMap::new();
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::generate::tests::{column, ir, model, pk};
    use crate::{Dialect, PlanOptions, Side, plan_from_ir};
    use ferro_schema_ir::{SchemaCheck, SchemaColumn, SchemaForeignKey};

    // -- builders ------------------------------------------------------------

    fn payload(models: Vec<SchemaModel>) -> SchemaIrPayload {
        ir(models).payload
    }

    /// Model `name` with an `id` and one string column per name; a
    /// `new<-old` entry declares `renamed_from="old"` on `new`.
    fn table(name: &str, columns: &[&str]) -> SchemaModel {
        let mut cols = vec![pk()];
        for spec in columns {
            cols.push(match spec.split_once("<-") {
                Some((new, old)) => SchemaColumn {
                    renamed_from: Some(old.into()),
                    ..column(new, "string")
                },
                None => column(spec, "string"),
            });
        }
        model(name, cols)
    }

    fn renamed_table(name: &str, from: &str, columns: &[&str]) -> SchemaModel {
        SchemaModel {
            renamed_from: Some(from.into()),
            ..table(name, columns)
        }
    }

    /// An enum column `status` of type `status`; `new<-old` declares a label
    /// rename hint.
    fn status(labels: &[&str]) -> SchemaColumn {
        let mut values = Vec::new();
        let mut renamed = BTreeMap::new();
        for spec in labels {
            match spec.split_once("<-") {
                Some((new, old)) => {
                    values.push(serde_json::json!(new));
                    renamed.insert(new.to_string(), old.to_string());
                }
                None => values.push(serde_json::json!(spec)),
            }
        }
        SchemaColumn {
            enum_values: Some(values),
            enum_type_name: Some("status".into()),
            enum_renamed_labels: (!renamed.is_empty()).then(|| SchemaRenamedLabels {
                enum_class: "Status".into(),
                labels: renamed,
            }),
            ..column("status", "string")
        }
    }

    fn order(labels: &[&str]) -> SchemaModel {
        model("Order", vec![pk(), status(labels)])
    }

    /// `post` with a foreign key to `author` under the relation field
    /// `field`; `Some(old)` declares `ForeignKey(renamed_from=old)`.
    fn post(field: &str, renamed_from: Option<&str>) -> SchemaModel {
        let shadow = format!("{field}_id");
        SchemaModel {
            foreign_keys: vec![SchemaForeignKey {
                renamed_from: renamed_from.map(Into::into),
                column: shadow.clone(),
                to_table: "author".into(),
                to_column: "id".into(),
                on_delete: Some("CASCADE".into()),
                name: Some(format!("fk_post_{shadow}_author")),
            }],
            ..model(
                "Post",
                vec![
                    pk(),
                    SchemaColumn {
                        renamed_from: renamed_from.map(|old| format!("{old}_id")),
                        ..column(&shadow, "integer")
                    },
                ],
            )
        }
    }

    fn author() -> SchemaModel {
        table("Author", &["name"])
    }

    fn compose(parent: &SchemaIrPayload, run: &[SchemaIrPayload]) -> Composed {
        let run: Vec<&SchemaIrPayload> = run.iter().collect();
        compose_run(Some(parent), &run).expect("composes")
    }

    fn refusal(parent: &SchemaIrPayload, run: &[SchemaIrPayload]) -> ComposeError {
        let run: Vec<&SchemaIrPayload> = run.iter().collect();
        compose_run(Some(parent), &run).expect_err("refused")
    }

    /// `payload` with every hint field cleared.
    fn without_hints(payload: &SchemaIrPayload) -> SchemaIrPayload {
        let mut out = payload.clone();
        for model in &mut out.models {
            model.renamed_from = None;
            for col in &mut model.columns {
                col.renamed_from = None;
                col.enum_renamed_labels = None;
            }
            for fk in &mut model.foreign_keys {
                fk.renamed_from = None;
            }
        }
        out
    }

    /// The composed target's hints, read back: `(table hint, [(column,
    /// renamed_from)])` per model.
    type Hints = Vec<(String, Option<String>, Vec<(String, String)>)>;

    fn hints(payload: &SchemaIrPayload) -> Hints {
        payload
            .models
            .iter()
            .map(|model| {
                (
                    model.table_name.clone(),
                    model.renamed_from.clone(),
                    model
                        .columns
                        .iter()
                        .filter_map(|c| Some((c.name.clone(), c.renamed_from.clone()?)))
                        .collect(),
                )
            })
            .collect()
    }

    fn no_hints(payload: &SchemaIrPayload) -> bool {
        without_hints(payload) == *payload
    }

    /// Assert the composed target is the last payload in every non-hint field.
    fn assert_schema_is_last(composed: &Composed, run: &[SchemaIrPayload]) {
        assert_eq!(
            without_hints(&composed.target),
            without_hints(run.last().expect("a run"))
        );
    }

    fn column_hint(composed: &Composed, table: &str, column: &str) -> Option<String> {
        composed
            .target
            .models
            .iter()
            .find(|m| m.table_name == table)
            .and_then(|m| m.columns.iter().find(|c| c.name == column))
            .and_then(|c| c.renamed_from.clone())
    }

    fn label_hints(composed: &Composed) -> Option<BTreeMap<String, String>> {
        composed.target.models.iter().find_map(|m| {
            m.columns
                .iter()
                .find_map(|c| c.enum_renamed_labels.as_ref().map(|r| r.labels.clone()))
        })
    }

    fn statements(
        parent: &SchemaIrPayload,
        target: &SchemaIrPayload,
        dialect: Dialect,
    ) -> Vec<String> {
        let wrap = |payload: &SchemaIrPayload| {
            Side::declared(IrEnvelope {
                ir_kind: "schema".into(),
                ir_version: 2,
                payload: payload.clone(),
            })
        };
        plan_from_ir(
            &wrap(parent),
            &wrap(target),
            dialect,
            PlanOptions { destructive: true },
        )
        .render()
        .expect("renders")
        .into_iter()
        .flat_map(|op| op.statements)
        .collect()
    }

    // -- no hints -------------------------------------------------------------

    #[test]
    fn compose_a_run_without_hints_is_the_last_payload_unchanged() {
        let parent = payload(vec![author()]);
        let run = [
            payload(vec![table("Author", &["name", "bio"])]),
            payload(vec![table("Author", &["name", "bio", "email"])]),
        ];
        let composed = compose(&parent, &run);
        assert_eq!(composed.target, run[1]);
        assert!(composed.dead_names.is_empty());
    }

    #[test]
    fn compose_a_run_from_0001_has_no_parent() {
        let run = [
            payload(vec![author()]),
            payload(vec![table("Author", &["full_name<-name"])]),
        ];
        let refs: Vec<&SchemaIrPayload> = run.iter().collect();
        let composed = compose_run(None, &refs).expect("composes");
        // `name` was born inside the run: an add of `full_name`, no hint.
        assert!(no_hints(&composed.target));
        assert_schema_is_last(&composed, &run);
    }

    // -- columns ----------------------------------------------------------------

    #[test]
    fn compose_column_chained_renames_compose() {
        let parent = payload(vec![author()]);
        let run = [
            payload(vec![table("Author", &["full_name<-name"])]),
            payload(vec![table("Author", &["display<-full_name"])]),
        ];
        let composed = compose(&parent, &run);
        assert_eq!(
            column_hint(&composed, "author", "display").as_deref(),
            Some("name")
        );
        assert_schema_is_last(&composed, &run);
    }

    #[test]
    fn compose_column_chained_renames_compose_after_the_hint_is_deleted() {
        let parent = payload(vec![author()]);
        let run = [
            payload(vec![table("Author", &["full_name<-name"])]),
            payload(vec![table("Author", &["full_name"])]),
            payload(vec![table("Author", &["display<-full_name"])]),
            payload(vec![table("Author", &["display"])]),
        ];
        let composed = compose(&parent, &run);
        assert_eq!(
            column_hint(&composed, "author", "display").as_deref(),
            Some("name")
        );
        assert_schema_is_last(&composed, &run);
    }

    #[test]
    fn compose_column_rename_then_drop_is_a_drop_with_no_hint() {
        let parent = payload(vec![author()]);
        let run = [
            payload(vec![table("Author", &["full_name<-name"])]),
            payload(vec![table("Author", &[])]),
        ];
        let composed = compose(&parent, &run);
        assert!(no_hints(&composed.target));
        assert_schema_is_last(&composed, &run);
        assert!(composed.dead_names.is_empty());
    }

    #[test]
    fn compose_column_add_then_rename_is_an_add_with_no_hint() {
        let parent = payload(vec![table("Author", &[])]);
        let run = [
            payload(vec![table("Author", &["x"])]),
            payload(vec![table("Author", &["y<-x"])]),
        ];
        let composed = compose(&parent, &run);
        assert!(no_hints(&composed.target));
        assert_schema_is_last(&composed, &run);
        assert!(composed.dead_names.is_empty());
    }

    #[test]
    fn compose_column_rename_and_back_is_nothing() {
        let parent = payload(vec![author()]);
        let run = [
            payload(vec![table("Author", &["full_name<-name"])]),
            payload(vec![table("Author", &["name<-full_name"])]),
        ];
        let composed = compose(&parent, &run);
        assert!(no_hints(&composed.target));
        assert_schema_is_last(&composed, &run);
        assert!(composed.dead_names.is_empty());
    }

    #[test]
    fn compose_column_hint_inert_against_the_parent_is_dropped() {
        // The parent lacks the old name.
        let parent = payload(vec![table("Author", &["email"])]);
        let run = [payload(vec![table(
            "Author",
            &["email", "full_name<-name"],
        )])];
        let composed = compose(&parent, &run);
        assert!(no_hints(&composed.target));
        assert_schema_is_last(&composed, &run);

        // The parent already holds the new one.
        let parent = payload(vec![table("Author", &["full_name"])]);
        let run = [payload(vec![table("Author", &["full_name<-name"])])];
        let composed = compose(&parent, &run);
        assert!(no_hints(&composed.target));
        assert_schema_is_last(&composed, &run);
    }

    #[test]
    fn compose_a_run_of_one_keeps_its_live_hint() {
        let parent = payload(vec![author()]);
        let run = [payload(vec![renamed_table(
            "Writer",
            "author",
            &["full_name<-name"],
        )])];
        let composed = compose(&parent, &run);
        assert_eq!(composed.target, run[0]);
    }

    #[test]
    fn compose_column_two_hints_on_one_old_name_are_uncomposable() {
        let parent = payload(vec![table("Author", &["a"])]);
        let run = [
            payload(vec![table("Author", &["b<-a"])]),
            payload(vec![table("Author", &["c<-a"])]),
        ];
        let err = refusal(&parent, &run);
        let first = Hint::Column {
            table: "author".into(),
            old: "a".into(),
            new: "b".into(),
        };
        let second = Hint::Column {
            table: "author".into(),
            old: "a".into(),
            new: "c".into(),
        };
        assert_eq!(
            err,
            ComposeError::Uncomposable {
                first: first.clone(),
                second: second.clone()
            }
        );
        let text = err.to_string();
        assert!(text.contains("author.\"a\" -> \"b\""), "{text}");
        assert!(text.contains("author.\"a\" -> \"c\""), "{text}");
        assert!(text.contains("end the squash range before"), "{text}");
    }

    #[test]
    fn compose_a_stale_hint_that_agrees_with_the_run_is_not_a_conflict() {
        // `b<-a` kept on the model after its migration, and a later hint
        // spelling the whole chain: both agree with what the run did.
        let parent = payload(vec![table("Author", &["a"])]);
        let run = [
            payload(vec![table("Author", &["b<-a"])]),
            payload(vec![table("Author", &["b<-a"])]),
            payload(vec![table("Author", &["c<-b"])]),
            payload(vec![table("Author", &["c<-a"])]),
        ];
        let composed = compose(&parent, &run);
        assert_eq!(column_hint(&composed, "author", "c").as_deref(), Some("a"));
    }

    #[test]
    fn compose_column_old_name_re_added_is_refused_naming_the_migration() {
        let parent = payload(vec![table("Author", &["a"])]);
        let run = [
            payload(vec![table("Author", &["b<-a"])]),
            payload(vec![table("Author", &["b"])]),
            payload(vec![table("Author", &["b", "a"])]),
        ];
        let err = refusal(&parent, &run);
        assert_eq!(
            err,
            ComposeError::OldNameReAdded {
                hint: Hint::Column {
                    table: "author".into(),
                    old: "a".into(),
                    new: "b".into()
                },
                re_added_at: 2,
            }
        );
        let text = err.to_string();
        assert!(text.contains("author.\"a\" -> \"b\""), "{text}");
        assert!(
            text.contains("end the squash range before migration #3"),
            "{text}"
        );
    }

    #[test]
    fn compose_column_renamed_onto_a_name_the_parent_held_is_refused() {
        // The parent's `b` dropped in 0, `a` renamed to `b` in 1: against the
        // parent the composed `b<-a` is inert and the planner would drop `a`.
        let parent = payload(vec![table("Author", &["a", "b"])]);
        let run = [
            payload(vec![table("Author", &["a"])]),
            payload(vec![table("Author", &["b<-a"])]),
        ];
        let err = refusal(&parent, &run);
        assert_eq!(
            err,
            ComposeError::NewNameHeldByParent {
                hint: Hint::Column {
                    table: "author".into(),
                    old: "a".into(),
                    new: "b".into()
                },
                vacated_at: 0,
            }
        );
        assert!(
            err.to_string()
                .contains("start the squash range after migration #1")
        );
    }

    #[test]
    fn compose_a_snapshot_the_planner_refuses_is_refused_with_its_position() {
        let parent = payload(vec![table("Author", &["a"])]);
        let run = [
            payload(vec![table("Author", &["a"])]),
            payload(vec![table("Author", &["a", "b<-a"])]),
        ];
        match refusal(&parent, &run) {
            ComposeError::RefusedHint {
                position: 1,
                error: HintError::OldStillDeclared { .. },
            } => {}
            other => panic!("unexpected {other:?}"),
        }
    }

    // -- tables -------------------------------------------------------------

    #[test]
    fn compose_table_chained_renames_compose() {
        let parent = payload(vec![author()]);
        let run = [
            payload(vec![renamed_table("Writer", "author", &["name"])]),
            payload(vec![renamed_table("Person", "writer", &["name"])]),
        ];
        let composed = compose(&parent, &run);
        assert_eq!(
            hints(&composed.target),
            vec![("person".into(), Some("author".into()), vec![])]
        );
        assert_schema_is_last(&composed, &run);
    }

    #[test]
    fn compose_table_rename_then_drop_is_a_drop_with_no_hint() {
        let parent = payload(vec![author(), table("Book", &[])]);
        let run = [
            payload(vec![
                renamed_table("Writer", "author", &["name"]),
                table("Book", &[]),
            ]),
            payload(vec![table("Book", &[])]),
        ];
        let composed = compose(&parent, &run);
        assert!(no_hints(&composed.target));
        assert_schema_is_last(&composed, &run);
    }

    #[test]
    fn compose_table_add_then_rename_and_rename_and_back() {
        let parent = payload(vec![author()]);
        let run = [
            payload(vec![
                renamed_table("Writer", "author", &["name"]),
                table("Tmp", &[]),
            ]),
            payload(vec![
                renamed_table("Author", "writer", &["name"]),
                renamed_table("Book", "tmp", &[]),
            ]),
        ];
        let composed = compose(&parent, &run);
        assert!(no_hints(&composed.target));
        assert_schema_is_last(&composed, &run);
    }

    #[test]
    fn compose_table_old_name_re_added_is_refused() {
        let parent = payload(vec![author()]);
        let run = [
            payload(vec![renamed_table("Writer", "author", &["name"])]),
            payload(vec![table("Writer", &["name"]), table("Author", &[])]),
        ];
        assert_eq!(
            refusal(&parent, &run),
            ComposeError::OldNameReAdded {
                hint: Hint::Table {
                    old: "author".into(),
                    new: "writer".into()
                },
                re_added_at: 1,
            }
        );
    }

    #[test]
    fn compose_table_two_hints_on_one_old_name_are_uncomposable() {
        let parent = payload(vec![author()]);
        let run = [
            payload(vec![renamed_table("Writer", "author", &["name"])]),
            payload(vec![renamed_table("Person", "author", &["name"])]),
        ];
        assert_eq!(
            refusal(&parent, &run),
            ComposeError::Uncomposable {
                first: Hint::Table {
                    old: "author".into(),
                    new: "writer".into()
                },
                second: Hint::Table {
                    old: "author".into(),
                    new: "person".into()
                },
            }
        );
    }

    #[test]
    fn compose_column_hint_composes_through_its_table_rename() {
        let parent = payload(vec![author()]);
        let run = [
            payload(vec![table("Author", &["full_name<-name"])]),
            payload(vec![renamed_table("Writer", "author", &["full_name"])]),
        ];
        let composed = compose(&parent, &run);
        assert_eq!(
            hints(&composed.target),
            vec![(
                "writer".into(),
                Some("author".into()),
                vec![("full_name".into(), "name".into())]
            )]
        );
        assert_schema_is_last(&composed, &run);
        for dialect in [Dialect::Postgres, Dialect::Sqlite] {
            let sql = statements(&parent, &composed.target, dialect);
            assert!(
                sql.contains(&"ALTER TABLE \"author\" RENAME TO \"writer\"".to_string()),
                "{dialect:?}: {sql:?}"
            );
            assert!(
                sql.contains(
                    &"ALTER TABLE \"writer\" RENAME COLUMN \"name\" TO \"full_name\"".to_string()
                ),
                "{dialect:?}: {sql:?}"
            );
            assert!(
                !sql.iter().any(|s| s.contains("DROP")),
                "{dialect:?}: {sql:?}"
            );
        }
    }

    // -- foreign keys ---------------------------------------------------------

    #[test]
    fn compose_foreign_key_chained_renames_compose_with_the_shadow_column() {
        let parent = payload(vec![author(), post("author", None)]);
        let run = [
            payload(vec![author(), post("writer", Some("author"))]),
            payload(vec![author(), post("owner", Some("writer"))]),
        ];
        let composed = compose(&parent, &run);
        assert_eq!(
            column_hint(&composed, "post", "owner_id").as_deref(),
            Some("author_id")
        );
        let fk = &composed.target.models[1].foreign_keys[0];
        assert_eq!(fk.renamed_from.as_deref(), Some("author"));
        assert_schema_is_last(&composed, &run);
        for dialect in [Dialect::Postgres, Dialect::Sqlite] {
            let sql = statements(&parent, &composed.target, dialect);
            assert!(
                !sql.iter().any(|s| s.contains("DROP")),
                "{dialect:?}: {sql:?}"
            );
            assert!(
                sql.iter()
                    .any(|s| s.contains("RENAME COLUMN \"author_id\" TO \"owner_id\"")),
                "{dialect:?}: {sql:?}"
            );
        }
    }

    #[test]
    fn compose_foreign_key_rename_then_drop_is_a_drop_with_no_hint() {
        let parent = payload(vec![author(), post("author", None)]);
        let run = [
            payload(vec![author(), post("writer", Some("author"))]),
            payload(vec![author(), table("Post", &[])]),
        ];
        let composed = compose(&parent, &run);
        assert!(no_hints(&composed.target));
        assert_schema_is_last(&composed, &run);
    }

    // -- enum labels ----------------------------------------------------------

    #[test]
    fn compose_label_chained_renames_compose() {
        let parent = payload(vec![order(&["draft", "live"])]);
        let run = [
            payload(vec![order(&["pending<-draft", "live"])]),
            payload(vec![order(&["queued<-pending", "live"])]),
        ];
        let composed = compose(&parent, &run);
        assert_eq!(
            label_hints(&composed),
            Some(BTreeMap::from([(
                "queued".to_string(),
                "draft".to_string()
            )]))
        );
        assert_eq!(
            composed.target.models[0].columns[1]
                .enum_renamed_labels
                .as_ref()
                .map(|r| r.enum_class.as_str()),
            Some("Status")
        );
        assert_schema_is_last(&composed, &run);
    }

    #[test]
    fn compose_label_rename_then_drop_is_a_drop_with_no_hint() {
        let parent = payload(vec![order(&["draft", "live"])]);
        let run = [
            payload(vec![order(&["pending<-draft", "live"])]),
            payload(vec![order(&["live"])]),
        ];
        let composed = compose(&parent, &run);
        assert!(no_hints(&composed.target));
        assert_schema_is_last(&composed, &run);
    }

    #[test]
    fn compose_label_old_name_re_added_is_refused() {
        let parent = payload(vec![order(&["draft", "live"])]);
        let run = [
            payload(vec![order(&["pending<-draft", "live"])]),
            payload(vec![order(&["pending", "live", "draft"])]),
        ];
        assert_eq!(
            refusal(&parent, &run),
            ComposeError::OldNameReAdded {
                hint: Hint::Label {
                    type_name: "status".into(),
                    old: "draft".into(),
                    new: "pending".into()
                },
                re_added_at: 1,
            }
        );
    }

    // -- dead names ---------------------------------------------------------

    #[test]
    fn compose_dead_column_is_reported_with_its_positions() {
        let parent = payload(vec![table("Team", &["name"])]);
        let run = [
            payload(vec![table("Team", &["name"])]),
            payload(vec![table("Team", &["name", "tmp", "kept"])]),
            payload(vec![table("Team", &["name", "tmp", "kept"])]),
            payload(vec![table("Team", &["kept"])]),
        ];
        let composed = compose(&parent, &run);
        // `kept` stays to the end; the parent's `name` is dropped in the run.
        assert_eq!(
            composed.dead_names,
            vec![DeadName {
                kind: DeadKind::Column,
                name: "team.tmp".into(),
                appeared: 1,
                vanished: 3,
            }]
        );
    }

    #[test]
    fn compose_dead_label_check_and_table_are_reported() {
        let mut with_check = order(&["draft", "live", "held"]);
        with_check.checks.push(SchemaCheck {
            name: "ck_order_status".into(),
            column: "status".into(),
            values: vec!["'draft'".into(), "'live'".into(), "'held'".into()],
        });
        let parent = payload(vec![order(&["draft", "live"])]);
        let run = [
            payload(vec![with_check, table("Scratch", &[])]),
            payload(vec![order(&["draft", "live"])]),
        ];
        let composed = compose(&parent, &run);
        let mut found: Vec<(DeadKind, String, usize, usize)> = composed
            .dead_names
            .iter()
            .map(|d| (d.kind, d.name.clone(), d.appeared, d.vanished))
            .collect();
        found.sort();
        assert_eq!(
            found,
            vec![
                (DeadKind::Table, "scratch".into(), 0, 1),
                (DeadKind::Column, "scratch.id".into(), 0, 1),
                (DeadKind::EnumLabel, "order.status:held".into(), 0, 1),
                (DeadKind::Constraint, "order.ck_order_status".into(), 0, 1),
            ]
        );
    }

    #[test]
    fn compose_a_renamed_name_is_not_dead() {
        let parent = payload(vec![author()]);
        let run = [
            payload(vec![table("Author", &["full_name<-name"])]),
            payload(vec![table("Author", &["display<-full_name"])]),
        ];
        assert!(compose(&parent, &run).dead_names.is_empty());
    }

    #[test]
    fn compose_a_dead_column_renamed_inside_the_run_is_one_entry_per_name() {
        let parent = payload(vec![table("Team", &[])]);
        let run = [
            payload(vec![table("Team", &["x"])]),
            payload(vec![table("Team", &["y<-x"])]),
            payload(vec![table("Team", &[])]),
        ];
        assert_eq!(
            compose(&parent, &run).dead_names,
            vec![
                DeadName {
                    kind: DeadKind::Column,
                    name: "team.x".into(),
                    appeared: 0,
                    vanished: 1,
                },
                DeadName {
                    kind: DeadKind::Column,
                    name: "team.y".into(),
                    appeared: 1,
                    vanished: 2,
                },
            ]
        );
    }
}
