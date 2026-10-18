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
use crate::snapshot::CURRENT_SCHEMA_IR_VERSION;
use ferro_ddl_lowering::fk_name;
use ferro_schema_ir::{
    IrEnvelope, SchemaColumn, SchemaIrPayload, SchemaModel, SchemaRenamedLabels,
};
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
                 later renamed_from to the name the earlier migration left, or start the \
                 squash range at the migration that declares it, leaving the earlier rename \
                 below the squash",
                describe(first),
                describe(second)
            ),
            ComposeError::OldNameReAdded { hint, re_added_at } => write!(
                f,
                "the run renames {}, then declares \"{}\" again from migration #{} of the \
                 range: one migration cannot rename a name and keep it (ADR-0032); start the \
                 squash range at migration #{}, leaving the rename below the squash",
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
                 refuses ({error}); a schema snapshot is never re-recorded (ADR-0030): restore \
                 that migration's ir.json from version control, or start the squash range after \
                 migration #{}",
                position + 1,
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
    let below = parent.cloned().unwrap_or_else(empty);
    if run.is_empty() {
        return Ok(Composed {
            target: without_hints(&below),
            dead_names: Vec::new(),
        });
    }
    // Side 0 is the parent, side `i + 1` the run's snapshot `i`; step `p`
    // goes from side `p - 1` to side `p`.
    let mut sides: Vec<&SchemaIrPayload> = vec![&below];
    sides.extend(run.iter().copied());
    let mut steps: Vec<Vec<Hint>> = vec![Vec::new()];
    for p in 1..sides.len() {
        let hints =
            live_hints(sides[p - 1], sides[p]).map_err(|error| ComposeError::RefusedHint {
                position: p - 1,
                error,
            })?;
        steps.push(hints);
    }
    let lineage = Lineage::follow(&sides, &steps);
    lineage.refuse_disagreeing_declarations(&sides, &steps)?;
    let target = lineage.composed_target(&sides)?;
    lineage.refuse_re_added(&sides, &target)?;
    lineage.refuse_held_by_parent(&target)?;
    Ok(Composed {
        target,
        dead_names: lineage.dead_names(sides.len() - 1),
    })
}

fn empty() -> SchemaIrPayload {
    SchemaIrPayload {
        dialect_agnostic: true,
        models: Vec::new(),
    }
}

/// `payload` with every rename hint field cleared.
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

/// One schema object of a snapshot, by the names that snapshot gives it.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Key {
    Table(String),
    Column(String, String),
    Label(String, String, String),
    Constraint(String, String),
}

impl Key {
    fn kind(&self) -> DeadKind {
        match self {
            Key::Table(_) => DeadKind::Table,
            Key::Column(..) => DeadKind::Column,
            Key::Label(..) => DeadKind::EnumLabel,
            Key::Constraint(..) => DeadKind::Constraint,
        }
    }

    /// `table`, `table.column`, `table.column:label` or `table.constraint`.
    fn name(&self) -> String {
        match self {
            Key::Table(table) => table.clone(),
            Key::Column(table, column) | Key::Constraint(table, column) => {
                format!("{table}.{column}")
            }
            Key::Label(table, column, label) => format!("{table}.{column}:{label}"),
        }
    }

    /// The object's own name, without its owners'.
    fn leaf(&self) -> &str {
        match self {
            Key::Table(name)
            | Key::Column(_, name)
            | Key::Label(_, _, name)
            | Key::Constraint(_, name) => name,
        }
    }
}

/// Every object `model` declares, in a fixed order: the same order for a
/// model and its [`renamed_snapshot`], which keeps each item in place.
fn model_keys(model: &SchemaModel) -> Vec<Key> {
    let table = &model.table_name;
    let mut keys = vec![Key::Table(table.clone())];
    for col in &model.columns {
        keys.push(Key::Column(table.clone(), col.name.clone()));
        if let Some((_, labels)) = enum_declaration(col) {
            for label in labels {
                keys.push(Key::Label(table.clone(), col.name.clone(), label));
            }
        }
    }
    let constraint = |name: &str| Key::Constraint(table.clone(), name.to_string());
    for fk in &model.foreign_keys {
        let name = fk
            .name
            .clone()
            .unwrap_or_else(|| fk_name(table, &fk.column, &fk.to_table));
        keys.push(constraint(&name));
    }
    keys.extend(model.indexes.iter().map(|i| constraint(&i.name)));
    keys.extend(model.uniques.iter().map(|u| constraint(&u.name)));
    keys.extend(model.checks.iter().map(|c| constraint(&c.name)));
    keys.extend(model.table_checks.iter().map(|c| constraint(&c.name)));
    keys
}

/// The column `column` of `table` in `side`.
fn find_column<'a>(
    side: &'a SchemaIrPayload,
    table: &str,
    column: &str,
) -> Option<&'a SchemaColumn> {
    side.models
        .iter()
        .find(|m| m.table_name == table)?
        .columns
        .iter()
        .find(|c| c.name == column)
}

/// The enum type of `table.column` in `side`.
fn enum_type(side: &SchemaIrPayload, table: &str, column: &str) -> Option<String> {
    find_column(side, table, column)
        .and_then(enum_declaration)
        .map(|(type_name, _)| type_name)
}

/// Whether `side` declares the label `label` on `table.column`.
fn declares_label(side: &SchemaIrPayload, table: &str, column: &str, label: &str) -> bool {
    find_column(side, table, column)
        .and_then(enum_declaration)
        .is_some_and(|(_, labels)| labels.iter().any(|l| l == label))
}

/// Every object of the run followed from side to side, as the planner sees
/// one step: the previous side with the step's live hints applied
/// ([`renamed_snapshot`]) names each object as the next side names it.
struct Lineage {
    /// Per side: each key and the object it names.
    at: Vec<BTreeMap<Key, usize>>,
    /// Per object: its key on each side it exists on (contiguous sides).
    objects: Vec<BTreeMap<usize, Key>>,
}

impl Lineage {
    fn follow(sides: &[&SchemaIrPayload], steps: &[Vec<Hint>]) -> Lineage {
        let mut lineage = Lineage {
            at: Vec::with_capacity(sides.len()),
            objects: Vec::new(),
        };
        for (p, side) in sides.iter().enumerate() {
            let mut carried: BTreeMap<Key, usize> = BTreeMap::new();
            if p > 0 {
                let previous = IrEnvelope {
                    ir_kind: "schema".into(),
                    ir_version: CURRENT_SCHEMA_IR_VERSION,
                    payload: sides[p - 1].clone(),
                };
                let renamed = renamed_snapshot(&previous, &steps[p]);
                for (before, after) in sides[p - 1].models.iter().zip(&renamed.payload.models) {
                    for (old, new) in model_keys(before).into_iter().zip(model_keys(after)) {
                        if let Some(&id) = lineage.at[p - 1].get(&old) {
                            carried.entry(new).or_insert(id);
                        }
                    }
                }
            }
            let mut here: BTreeMap<Key, usize> = BTreeMap::new();
            for model in &side.models {
                for key in model_keys(model) {
                    if here.contains_key(&key) {
                        continue;
                    }
                    let id = match carried.get(&key) {
                        Some(&id) => id,
                        None => {
                            lineage.objects.push(BTreeMap::new());
                            lineage.objects.len() - 1
                        }
                    };
                    lineage.objects[id].insert(p, key.clone());
                    here.insert(key, id);
                }
            }
            lineage.at.push(here);
        }
        lineage
    }

    /// The key of object `id` on side `p`.
    fn key(&self, id: usize, p: usize) -> Option<&Key> {
        self.objects[id].get(&p)
    }

    /// The object `key` names on side `p`.
    fn id(&self, p: usize, key: &Key) -> Option<usize> {
        self.at[p].get(key).copied()
    }

    /// The parent's key of the object `key` names on the last side.
    fn origin(&self, key: &Key) -> Option<&Key> {
        let last = self.at.len() - 1;
        self.id(last, key).and_then(|id| self.key(id, 0))
    }

    /// What the object `key` names on side `j` is called on side `p`.
    fn became(&self, j: usize, key: Key, p: usize) -> Option<&Key> {
        self.id(j, &key).and_then(|id| self.key(id, p))
    }

    /// Refuse a declaration that claims an old name an earlier live hint
    /// already renamed away, for something else than what that rename
    /// became: the run renamed one way and declares another.
    fn refuse_disagreeing_declarations(
        &self,
        sides: &[&SchemaIrPayload],
        steps: &[Vec<Hint>],
    ) -> Result<(), ComposeError> {
        let disagree = |first: &Hint, second: Hint| ComposeError::Uncomposable {
            first: first.clone(),
            second,
        };
        for p in 2..sides.len() {
            let earlier = || (1..p).flat_map(|j| steps[j].iter().map(move |h| (j, h)));
            for model in &sides[p].models {
                let table = &model.table_name;
                if let Some(old) = &model.renamed_from {
                    let declared = Hint::Table {
                        old: old.clone(),
                        new: table.clone(),
                    };
                    if !steps[p].contains(&declared) {
                        for (j, live) in earlier() {
                            if let Hint::Table { old: o, new: n } = live
                                && o == old
                                && self.became(j, Key::Table(n.clone()), p)
                                    != Some(&Key::Table(table.clone()))
                            {
                                return Err(disagree(live, declared));
                            }
                        }
                    }
                }
                let table_id = self.id(p, &Key::Table(table.clone()));
                for col in &model.columns {
                    let here = Key::Column(table.clone(), col.name.clone());
                    if let Some(old) = &col.renamed_from {
                        let declared = Hint::Column {
                            table: table.clone(),
                            old: old.clone(),
                            new: col.name.clone(),
                        };
                        if !steps[p].contains(&declared) {
                            for (j, live) in earlier() {
                                if let Hint::Column {
                                    table: t,
                                    old: o,
                                    new: n,
                                } = live
                                    && o == old
                                    && self.id(j, &Key::Table(t.clone())) == table_id
                                    && self.became(j, Key::Column(t.clone(), n.clone()), p)
                                        != Some(&here)
                                {
                                    return Err(disagree(live, declared));
                                }
                            }
                        }
                    }
                    let (Some(renamed), Some((type_name, _))) =
                        (&col.enum_renamed_labels, enum_declaration(col))
                    else {
                        continue;
                    };
                    let col_id = self.id(p, &here);
                    for (new, old) in &renamed.labels {
                        let declared = Hint::Label {
                            type_name: type_name.clone(),
                            old: old.clone(),
                            new: new.clone(),
                        };
                        if steps[p].contains(&declared) {
                            continue;
                        }
                        let now = Key::Label(table.clone(), col.name.clone(), new.clone());
                        for (j, live) in earlier() {
                            let Hint::Label {
                                type_name: ty,
                                old: o,
                                new: n,
                            } = live
                            else {
                                continue;
                            };
                            let Some(Key::Column(t, c)) = col_id.and_then(|id| self.key(id, j))
                            else {
                                continue;
                            };
                            if o == old
                                && enum_type(sides[j], t, c).as_ref() == Some(ty)
                                && self.became(j, Key::Label(t.clone(), c.clone(), n.clone()), p)
                                    != Some(&now)
                            {
                                return Err(disagree(live, declared));
                            }
                        }
                    }
                }
            }
        }
        Ok(())
    }

    /// The last side with its hint fields rewritten: each object present on
    /// the parent and on the last side under another name carries one hint
    /// from its parent name.
    fn composed_target(&self, sides: &[&SchemaIrPayload]) -> Result<SchemaIrPayload, ComposeError> {
        let last = sides.len() - 1;
        let mut target = without_hints(sides[last]);
        // Per enum type (as the last side names it): new label -> old label,
        // and the enum class that declared them.
        let mut labels: BTreeMap<String, BTreeMap<String, String>> = BTreeMap::new();
        let mut classes: BTreeMap<String, String> = BTreeMap::new();
        for model in &mut target.models {
            let table = model.table_name.clone();
            if let Some(Key::Table(old)) = self.origin(&Key::Table(table.clone()))
                && *old != table
            {
                model.renamed_from = Some(old.clone());
            }
            let mut shadows: Vec<(String, Option<String>)> = Vec::new();
            for col in &mut model.columns {
                let here = Key::Column(table.clone(), col.name.clone());
                if let Some(Key::Column(_, old)) = self.origin(&here)
                    && *old != col.name
                {
                    col.renamed_from = Some(old.clone());
                    shadows.push((col.name.clone(), self.relation_hint(sides, &here)));
                }
                let Some((type_name, declared)) = enum_declaration(col) else {
                    continue;
                };
                for label in declared {
                    let key = Key::Label(table.clone(), col.name.clone(), label.clone());
                    let Some(Key::Label(_, _, old)) = self.origin(&key) else {
                        continue;
                    };
                    if *old == label {
                        continue;
                    }
                    let renamed = labels.entry(type_name.clone()).or_default();
                    if let Some(other) = renamed.get(&label)
                        && other != old
                    {
                        return Err(ComposeError::Uncomposable {
                            first: Hint::Label {
                                type_name: type_name.clone(),
                                old: other.clone(),
                                new: label.clone(),
                            },
                            second: Hint::Label {
                                type_name,
                                old: old.clone(),
                                new: label,
                            },
                        });
                    }
                    renamed.insert(label.clone(), old.clone());
                    if let Some(class) = self.enum_class(sides, &key) {
                        classes.entry(type_name.clone()).or_insert(class);
                    }
                }
            }
            // A foreign key's hint follows its shadow column's.
            for (column, field) in shadows {
                if let Some(fk) = model.foreign_keys.iter_mut().find(|fk| fk.column == column) {
                    fk.renamed_from = field;
                }
            }
        }
        // The IR carries an enum's declaration on every column of it.
        for model in &mut target.models {
            for col in &mut model.columns {
                let Some((type_name, _)) = enum_declaration(col) else {
                    continue;
                };
                if let (Some(renamed), Some(class)) =
                    (labels.get(&type_name), classes.get(&type_name))
                {
                    col.enum_renamed_labels = Some(SchemaRenamedLabels {
                        enum_class: class.clone(),
                        labels: renamed.clone(),
                    });
                }
            }
        }
        Ok(target)
    }

    /// The relation field the foreign key on the shadow column `column` (a
    /// key of the last side) was declared under on the parent: the
    /// `ForeignKey(renamed_from=…)` of the step that first renamed it.
    fn relation_hint(&self, sides: &[&SchemaIrPayload], column: &Key) -> Option<String> {
        let id = self.id(sides.len() - 1, column)?;
        let parent_name = self.key(id, 0)?.leaf();
        let p = (1..sides.len()).find(|&p| self.key(id, p).map(Key::leaf) != Some(parent_name))?;
        let Some(Key::Column(table, col)) = self.key(id, p) else {
            return None;
        };
        sides[p]
            .models
            .iter()
            .find(|m| &m.table_name == table)?
            .foreign_keys
            .iter()
            .find(|fk| &fk.column == col)?
            .renamed_from
            .clone()
    }

    /// The enum class that declared the latest rename of `label` (a key of
    /// the last side), as that side's column names it.
    fn enum_class(&self, sides: &[&SchemaIrPayload], label: &Key) -> Option<String> {
        let id = self.id(sides.len() - 1, label)?;
        (1..sides.len()).rev().find_map(|p| {
            let (Some(Key::Label(table, column, now)), Some(Key::Label(_, _, before))) =
                (self.key(id, p), self.key(id, p - 1))
            else {
                return None;
            };
            if now == before {
                return None;
            }
            find_column(sides[p], table, column)?
                .enum_renamed_labels
                .as_ref()
                .map(|r| r.enum_class.clone())
        })
    }

    /// Refuse a composed hint whose old name the last side declares again,
    /// naming the migration that re-declared it: the planner refuses such a
    /// hint (ADR-0032, [`refuse_hints`]).
    fn refuse_re_added(
        &self,
        sides: &[&SchemaIrPayload],
        target: &SchemaIrPayload,
    ) -> Result<(), ComposeError> {
        let last = sides.len() - 1;
        // The run index of the first side of the last stretch on which
        // `holds` is true. The rename's own side lacks the old name, so the
        // stretch starts after the parent.
        let since = |holds: &dyn Fn(usize) -> bool| {
            let mut p = last;
            while p > 1 && holds(p - 1) {
                p -= 1;
            }
            p - 1
        };
        let Err(error) = refuse_hints(target) else {
            return Ok(());
        };
        match error {
            HintError::OldStillDeclared {
                table,
                field: None,
                old,
            } => {
                let re_added_at = since(&|p| sides[p].models.iter().any(|m| m.table_name == old));
                Err(ComposeError::OldNameReAdded {
                    hint: Hint::Table { old, new: table },
                    re_added_at,
                })
            }
            HintError::OldStillDeclared {
                table,
                field: Some(field),
                old,
            } => {
                let table_id = self.id(last, &Key::Table(table.clone()));
                let re_added_at = since(&|p| {
                    matches!(
                        table_id.and_then(|id| self.key(id, p)),
                        Some(Key::Table(t)) if find_column(sides[p], t, &old).is_some()
                    )
                });
                Err(ComposeError::OldNameReAdded {
                    hint: Hint::Column {
                        table,
                        old,
                        new: field,
                    },
                    re_added_at,
                })
            }
            HintError::LabelStillDeclared {
                type_name,
                new,
                old,
                ..
            } => {
                let column = target.models.iter().find_map(|m| {
                    m.columns
                        .iter()
                        .find(|c| {
                            enum_declaration(c).is_some_and(|(ty, labels)| {
                                ty == type_name && labels.contains(&old)
                            })
                        })
                        .map(|c| Key::Column(m.table_name.clone(), c.name.clone()))
                });
                let col_id = column.and_then(|key| self.id(last, &key));
                let re_added_at = since(&|p| {
                    matches!(
                        col_id.and_then(|id| self.key(id, p)),
                        Some(Key::Column(t, c)) if declares_label(sides[p], t, c, &old)
                    )
                });
                Err(ComposeError::OldNameReAdded {
                    hint: Hint::Label {
                        type_name,
                        old,
                        new,
                    },
                    re_added_at,
                })
            }
            // Composed hints follow distinct objects to distinct names, so
            // no two claim one old name; were they to, the refusal is
            // reported, never dropped.
            error => Err(ComposeError::RefusedHint {
                position: last - 1,
                error,
            }),
        }
    }

    /// Refuse a composed hint onto a name the parent holds for another
    /// object: against the parent the hint is inert, and the planner would
    /// drop the object the run renamed.
    fn refuse_held_by_parent(&self, target: &SchemaIrPayload) -> Result<(), ComposeError> {
        let held = |key: Key, hint: Hint| match self.id(0, &key) {
            Some(id) => {
                // The first side on which the parent's object lost the name.
                let vacated = (1..self.at.len())
                    .find(|&p| self.key(id, p).map(Key::leaf) != Some(key.leaf()))
                    .unwrap_or(self.at.len() - 1);
                Err(ComposeError::NewNameHeldByParent {
                    hint,
                    vacated_at: vacated - 1,
                })
            }
            None => Ok(()),
        };
        for model in &target.models {
            let table = &model.table_name;
            let parent_table = model.renamed_from.as_ref().unwrap_or(table);
            if let Some(old) = &model.renamed_from {
                held(
                    Key::Table(table.clone()),
                    Hint::Table {
                        old: old.clone(),
                        new: table.clone(),
                    },
                )?;
            }
            for col in &model.columns {
                if let Some(old) = &col.renamed_from {
                    held(
                        Key::Column(parent_table.clone(), col.name.clone()),
                        Hint::Column {
                            table: table.clone(),
                            old: old.clone(),
                            new: col.name.clone(),
                        },
                    )?;
                }
                let (Some(renamed), Some((type_name, _)), Some(Key::Column(t, c))) = (
                    &col.enum_renamed_labels,
                    enum_declaration(col),
                    self.origin(&Key::Column(table.clone(), col.name.clone())),
                ) else {
                    continue;
                };
                for (new, old) in &renamed.labels {
                    held(
                        Key::Label(t.clone(), c.clone(), new.clone()),
                        Hint::Label {
                            type_name: type_name.clone(),
                            old: old.clone(),
                            new: new.clone(),
                        },
                    )?;
                }
            }
        }
        Ok(())
    }

    /// Every name an object born and gone inside the run held, with the
    /// run indexes where it appeared and vanished, in order of appearance.
    fn dead_names(&self, last: usize) -> Vec<DeadName> {
        let mut dead = Vec::new();
        for keys in &self.objects {
            if keys.contains_key(&0) || keys.contains_key(&last) {
                continue;
            }
            // Each stretch of sides under one name: (first, last, key).
            let mut held: Vec<(usize, usize, &Key)> = Vec::new();
            for (&p, key) in keys {
                match held.last_mut() {
                    Some((_, end, named)) if named.name() == key.name() => *end = p,
                    _ => held.push((p, p, key)),
                }
            }
            for (first, end, key) in held {
                dead.push(DeadName {
                    kind: key.kind(),
                    name: key.name(),
                    appeared: first - 1,
                    vanished: end,
                });
            }
        }
        dead.sort_by_key(|d| d.appeared);
        dead
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::generate::tests::{column, ir, model, pk};
    use crate::{Dialect, PlanOptions, Side, plan_from_ir};
    use ferro_schema_ir::{SchemaCheck, SchemaForeignKey};

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
        assert!(
            text.ends_with(
                "or start the squash range at the migration that declares it, leaving \
                 the earlier rename below the squash"
            ),
            "{text}"
        );
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
            text.ends_with(
                "start the squash range at migration #3, leaving the rename below the \
                 squash"
            ),
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
        let err = refusal(&parent, &run);
        assert!(
            matches!(
                err,
                ComposeError::RefusedHint {
                    position: 1,
                    error: HintError::OldStillDeclared { .. },
                }
            ),
            "{err:?}"
        );
        let text = err.to_string();
        assert!(
            text.contains(
                "restore that migration's ir.json from version control, or start the squash \
                 range after migration #2"
            ),
            "{text}"
        );
        assert!(!text.contains("rerecord"), "{text}");
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
