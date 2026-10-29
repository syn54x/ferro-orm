//! The one planner (ADR-0027, ADR-0041): every change between two SchemaIR
//! snapshots of a whole modelset, decided once, in execution order.
//!
//! The reconciliation pass, the migration generator, the drift check and the
//! Alembic bridge all ask [`plan_from_ir`] the same question — "what turns
//! `old` into `new`?" — and differ only in their sides (ADR-0050): each is a
//! [`Side`], a declared modelset or a live database read into an IR, with what
//! the IR cannot say (catalog CHECK bodies, validity flags, row policies as
//! `pg_policy` prints them, live enum labels) beside it as [`LiveFacts`].
//! Either side may be the target: a down ([`plan_down`]) is the planner run
//! from an up's after-side back to its before-side, scoped to the artifacts
//! the up touched — toward a live database for the Alembic bridge.
//!
//! Every op leaves with its [`OpVerdict`], computed once here: what is true
//! of it between the two sides on the dialect, which every door reads.
//!
//! Every decision is the `ferro_ddl_lowering` function the pass consumed
//! before this module existed (AGENTS.md § I-1 items 11–16); this module only
//! decides *where* in the plan each decision lands.

use crate::{
    Dialect, LiveCheckValidity, LiveFkValidity, LiveIndexValidity, MigrationOp, Plan, PlanOptions,
    emit,
};
use ferro_ddl_lowering::Report;
pub(crate) use ferro_ddl_lowering::and_list;
use ferro_ddl_lowering::{
    EnumTypeProvenance, LiveRowPolicy, LiveRowSecurity, ResolvedStorage, RowSecurityFlag,
    declared_check_body, declared_check_names, declared_row_policy_names, drifted_check_names,
    dropped_row_security_warning, enum_label_strings, enum_type_provenance,
    excess_row_security_flags, extra_check_names, extra_check_names_warning, extra_enum_labels,
    extra_enum_labels_warning, extra_row_policy_names_warning, ferro_manages_row_security,
    fk_action_from_str, fk_action_sql, fk_name, foreign_default_warning, foreign_fk_drift_warning,
    is_ferro_fk_name, is_ferro_row_policy_name, missing_check_names, missing_enum_labels,
    normalize_check_definition, normalize_row_policy_expr, plan_row_security_reconcile,
    quote_label, resolve_column_storage, row_policy_clauses, row_policy_command_token,
    schema_columns_storage_drift,
};
pub use ferro_ddl_lowering::{HintError, hint_refusal_warning, pending_table_rename_warning};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};

fn serde_default_true() -> bool {
    true
}

/// One live CHECK constraint as the catalog reports it. Its body is the
/// backend's own rendering — the IR carries exactly one body language, so a
/// live CHECK travels here rather than as a `SchemaCheck` (AGENTS.md § I-1).
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LiveCheckFact {
    /// Constraint name.
    pub name: String,
    /// The catalog definition (`pg_get_constraintdef` on Postgres, the
    /// inline `CHECK (…)` fragment on SQLite).
    pub definition: String,
    /// Whether the name follows ferro's `ck_` convention. Only a ferro-owned
    /// CHECK is ever rebuilt or dropped.
    #[serde(default)]
    pub ferro_owned: bool,
    /// `pg_constraint.convalidated`; always `true` on SQLite.
    #[serde(default = "serde_default_true")]
    pub validated: bool,
    /// The one column the check belongs to, when it belongs to one: on
    /// SQLite the column whose definition carries it inline (a table-level
    /// `CHECK` belongs to the table, whatever it reads); on Postgres the only
    /// column it reads (`pg_constraint.conkey`). Postgres drops such a check
    /// with its column, and SQLite drops an inline one with it.
    #[serde(default)]
    pub column: Option<String>,
    /// Postgres: the declared body and the catalog's, printed side by side
    /// (ADR-0060), when the live read had a declared check of this name to
    /// compare it with ([`printed_pair_checks`]). `None` on SQLite.
    #[serde(default)]
    pub printed: Option<PrintedPair>,
}

/// A Postgres rendering and the catalog's reprint of the same body, each as
/// Postgres printed it back through one `EXPLAIN (VERBOSE, FORMAT JSON)`
/// (ADR-0060): both passed through the same analysis, so equal strings are
/// the same expression.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct PrintedPair {
    /// The declared rendering, as Postgres printed it.
    pub declared: String,
    /// The catalog's reprint, as Postgres printed it.
    pub catalog: String,
}

/// One live generated column as the catalog reports it.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LiveGeneratedFact {
    /// The column.
    pub column: String,
    /// `STORED` (Postgres `attgenerated = 's'`, SQLite `hidden = 3`); a
    /// `VIRTUAL` one is never the column ferro creates.
    pub stored: bool,
    /// Postgres: `pg_get_expr` of the expression. SQLite: the column
    /// definition from its generated clause on, its range guard included,
    /// as `sqlite_master` stores it.
    pub catalog: String,
    /// Postgres: the declared rendering and `catalog`, printed side by side,
    /// when the live read had a declared generated column to compare it
    /// with. `None` on SQLite.
    #[serde(default)]
    pub printed: Option<PrintedPair>,
}

/// Everything about one live table the planner reads that is not IR.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LiveTableFacts {
    /// Every live CHECK on the table, ferro-owned or not.
    #[serde(default)]
    pub checks: Vec<LiveCheckFact>,
    /// Validity of every live named foreign key.
    #[serde(default)]
    pub foreign_keys: Vec<LiveFkValidity>,
    /// Validity of every live ferro-owned index.
    #[serde(default)]
    pub indexes: Vec<LiveIndexValidity>,
    /// The table's row-security flags and every policy on it, with bodies as
    /// the catalog prints them. The "off, no policies" state on SQLite.
    #[serde(default, serialize_with = "serialize_row_security")]
    pub row_security: LiveRowSecurity,
    /// Every live generated column.
    #[serde(default)]
    pub generated: Vec<LiveGeneratedFact>,
}

/// The facts a live database holds beside its IR, keyed by table: every live
/// table's checks, validity flags and row security, and every live native
/// enum type's labels. A live side ([`Side::live`]) carries an entry for each
/// table of its IR; a type absent from [`enum_labels`](Self::enum_labels)
/// reads its labels from the IR's columns.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LiveFacts {
    /// Live facts per table name.
    #[serde(default)]
    pub tables: BTreeMap<String, LiveTableFacts>,
    /// Every live native enum type's labels in enum sort order (Postgres).
    #[serde(default)]
    pub enum_labels: BTreeMap<String, Vec<String>>,
}

/// Why a [`Side`] cannot be built.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PlanError {
    /// The live side's facts carry no entry for a table its schema holds.
    MissingLiveFacts {
        /// The table.
        table: String,
    },
    /// A Postgres live generated column the declared side also declares
    /// generated carries no printed pair: the live read was not given that
    /// declaration.
    MissingPrintedPair {
        /// The table.
        table: String,
        /// The column.
        column: String,
    },
    /// A Postgres live ferro-owned check that [`printed_pair_checks`] gives a
    /// pair carries none: the live read was not given that declaration.
    MissingCheckPrintedPair {
        /// The table.
        table: String,
        /// The check.
        check: String,
    },
}

impl std::fmt::Display for PlanError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PlanError::MissingLiveFacts { table } => write!(
                f,
                "the live facts carry no entry for table '{table}', which the live schema \
                 holds: read the schema and its facts together, from one introspection \
                 (`_live_schema_ir`), or plan two declared snapshots without facts"
            ),
            PlanError::MissingPrintedPair { table, column } => write!(
                f,
                "the live facts carry no printed pair for the generated column \
                 '{table}.{column}': they were read against another declaration than the one \
                 planned toward. Read them again with `_core._live_schema_ir(using, \
                 declared_json)`, passing the same declared_json the plan is made toward, and \
                 plan with the facts it returns unchanged"
            ),
            PlanError::MissingCheckPrintedPair { table, check } => write!(
                f,
                "the live facts carry no printed pair for the CHECK constraint \
                 '{table}.{check}': they were read against another declaration than the one \
                 planned toward. Read them again with `_core._live_schema_ir(using, \
                 declared_json)`, passing the same declared_json the plan is made toward, and \
                 plan with the facts it returns unchanged"
            ),
        }
    }
}

impl std::error::Error for PlanError {}

/// One side of a plan (ADR-0050): a declared modelset, or a live database
/// read as its IR plus the facts introspection returned beside it. The
/// planner never asks which one it holds; it asks the side questions
/// (a table's facts, an enum type's labels, a check's or policy's body,
/// whether it proves ferro installed a table's row security, …), and each
/// adapter answers them its own way.
#[derive(Clone, Debug, PartialEq)]
pub struct Side(SideKind);

#[derive(Clone, Debug, PartialEq)]
enum SideKind {
    /// A modelset: every artifact on it is ferro's own declaration.
    Declared(IrEnvelope<SchemaIrPayload>),
    /// A database: its IR, and its facts for every table the IR holds.
    Live {
        ir: IrEnvelope<SchemaIrPayload>,
        facts: LiveFacts,
    },
}

/// A check's body as one side holds it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Body {
    /// The canonical expression, as ferro renders a declaration for the
    /// dialect it is written on ([`ferro_ddl_lowering::declared_check_body`]).
    Canonical,
    /// The catalog's text (`CHECK (…)`), restored as the catalog printed it.
    Catalog(String),
}

/// A row policy as one side holds it: its declaration (a live policy as a
/// raw one, its bodies as the catalog printed them) and the roles it
/// applies `TO` (none for a declaration, which never writes the clause).
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct PolicyBody {
    /// The policy, as `CREATE POLICY` writes it.
    pub(crate) policy: ferro_schema_ir::SchemaRowPolicy,
    /// `pg_policy.polroles` for a live policy.
    pub(crate) roles: Vec<String>,
}

impl Side {
    /// A declared modelset: the models, or a schema snapshot.
    pub fn declared(ir: IrEnvelope<SchemaIrPayload>) -> Side {
        Side(SideKind::Declared(ir))
    }

    /// A live database, read as its IR and the facts introspection returned
    /// beside it (`_live_schema_ir`).
    ///
    /// # Errors
    /// [`PlanError::MissingLiveFacts`] when `facts` carries no entry for a
    /// table `ir` holds: the side is refused when it is built, never halfway
    /// through a plan.
    pub fn live(ir: IrEnvelope<SchemaIrPayload>, facts: LiveFacts) -> Result<Side, PlanError> {
        if let Some(model) = ir
            .payload
            .models
            .iter()
            .find(|model| !facts.tables.contains_key(&model.table_name))
        {
            return Err(PlanError::MissingLiveFacts {
                table: model.table_name.clone(),
            });
        }
        Ok(Side(SideKind::Live { ir, facts }))
    }

    /// The side's IR.
    pub fn ir(&self) -> &IrEnvelope<SchemaIrPayload> {
        match &self.0 {
            SideKind::Declared(ir) | SideKind::Live { ir, .. } => ir,
        }
    }

    /// The model of `table`, when the side holds one.
    pub(crate) fn model(&self, table: &str) -> Option<&SchemaModel> {
        self.ir()
            .payload
            .models
            .iter()
            .find(|model| model.table_name == table)
    }

    /// The facts of one of the side's tables: a declaration's are read off
    /// it (every declared check present with its canonical body and valid,
    /// every foreign key and index valid, row security exactly as declared);
    /// a live table's are the introspected facts.
    pub(crate) fn table_facts(&self, model: &SchemaModel) -> Cow<'_, LiveTableFacts> {
        match &self.0 {
            SideKind::Declared(_) => Cow::Owned(declared_table_facts(model)),
            SideKind::Live { facts, .. } => facts
                .tables
                .get(&model.table_name)
                .map_or_else(|| Cow::Owned(LiveTableFacts::default()), Cow::Borrowed),
        }
    }

    /// One of the side's tables as introspection would report it, the shape
    /// the storage decision compares: a declared Postgres column whose
    /// storage resolves to a native enum reads back as one.
    pub(crate) fn table_view<'a>(
        &self,
        model: &'a SchemaModel,
        dialect: Dialect,
    ) -> Cow<'a, SchemaModel> {
        match &self.0 {
            SideKind::Declared(_) => declared_live_view(model, dialect),
            SideKind::Live { .. } => Cow::Borrowed(model),
        }
    }

    /// The labels of the native enum type `type_name`, when the side holds
    /// it: the declaration's, or the live database's.
    pub(crate) fn enum_labels(&self, type_name: &str) -> Option<Vec<String>> {
        let declared = || {
            declared_enum_types(&self.ir().payload.models)
                .labels
                .get(type_name)
                .cloned()
        };
        match &self.0 {
            SideKind::Declared(_) => declared(),
            SideKind::Live { facts, .. } => {
                facts.enum_labels.get(type_name).cloned().or_else(declared)
            }
        }
    }

    /// Every check of `model` as the side holds them, as `(name, body)`, in
    /// its order: a declaration's table checks then column checks, each with
    /// its canonical body; a live table's checks with the catalog's text.
    pub(crate) fn checks(&self, model: &SchemaModel) -> Vec<(String, Body)> {
        match &self.0 {
            SideKind::Declared(_) => declared_check_names(model)
                .into_iter()
                .map(|name| (name, Body::Canonical))
                .collect(),
            SideKind::Live { .. } => self
                .table_facts(model)
                .checks
                .iter()
                .map(|check| (check.name.clone(), Body::Catalog(check.definition.clone())))
                .collect(),
        }
    }

    /// The body of the check `name` on `table`, as the side holds it.
    pub(crate) fn check_body(&self, table: &str, name: &str) -> Option<Body> {
        let model = self.model(table)?;
        self.checks(model)
            .into_iter()
            .find(|(check, _)| check == name)
            .map(|(_, body)| body)
    }

    /// The name of `column`'s own check on `model` as the side holds it — the
    /// rider [`emit::column_riders`] names: a declaration's column check; a
    /// live table's check of that name the catalog reports on that column.
    /// A live table-level `CHECK` of the same name is the table's, never the
    /// column's.
    pub(crate) fn column_check(&self, model: &SchemaModel, column: &str) -> Option<String> {
        let view = match &self.0 {
            SideKind::Declared(_) => Cow::Borrowed(model),
            SideKind::Live { .. } => {
                let mut view = model.clone();
                view.checks = self
                    .table_facts(model)
                    .checks
                    .iter()
                    .filter_map(|check| {
                        Some(ferro_schema_ir::SchemaCheck {
                            name: check.name.clone(),
                            column: check.column.clone()?,
                            values: Vec::new(),
                        })
                    })
                    .collect();
                Cow::Owned(view)
            }
        };
        emit::column_riders(&view, column)
            .check
            .map(|check| check.name.clone())
    }

    /// `model` with its row security as the side holds it: a declaration's
    /// own, or a live table's flags and policies as a declaration would state
    /// them (each policy raw, its bodies as the catalog printed them). A live
    /// table whose row security is off declares none.
    pub(crate) fn declaration<'a>(&self, model: &'a SchemaModel) -> Cow<'a, SchemaModel> {
        if matches!(self.0, SideKind::Declared(_)) {
            return Cow::Borrowed(model);
        }
        let facts = self.table_facts(model);
        let live = &facts.row_security;
        let mut view = model.clone();
        view.row_security = live.enabled.then(|| ferro_schema_ir::SchemaRowSecurity {
            force: live.forced,
            policies: live
                .policies
                .iter()
                .filter_map(|policy| live_policy_body(policy).map(|body| body.policy))
                .collect(),
        });
        Cow::Owned(view)
    }

    /// The row policy `name` on `table`, as the side holds it.
    pub(crate) fn policy(&self, table: &str, name: &str) -> Option<PolicyBody> {
        let model = self.model(table)?;
        match &self.0 {
            SideKind::Declared(_) => model
                .row_security
                .as_ref()?
                .policies
                .iter()
                .find(|policy| policy.name == name)
                .map(|policy| PolicyBody {
                    policy: policy.clone(),
                    roles: Vec::new(),
                }),
            SideKind::Live { .. } => self
                .table_facts(model)
                .row_security
                .policies
                .iter()
                .find(|policy| policy.name == name)
                .and_then(live_policy_body),
        }
    }

    /// Whether the side is itself the proof that ferro installed a table's
    /// row security: a declaration is (ferro wrote what it declares); a live
    /// table proves it only with a ferro-named policy.
    pub(crate) fn proves_row_security_installed(&self) -> bool {
        matches!(self.0, SideKind::Declared(_))
    }

    /// Whether a policy body ferro cannot verify, planned from this side, is
    /// rebuilt: yes from a declaration (both texts are ferro's own copies of
    /// a declaration, so a difference is the author's edit); no from a live
    /// database, which only warns (ADR-0019).
    pub(crate) fn rebuilds_unverifiable_bodies(&self) -> bool {
        matches!(self.0, SideKind::Declared(_))
    }

    /// Whether a label this side's enum type holds and the target's drops is
    /// planned as its removal: yes from a declaration (#536); a live
    /// database's extra label is reported, never removed (ADR-0011).
    pub(crate) fn plans_label_removals(&self) -> bool {
        matches!(self.0, SideKind::Declared(_))
    }

    /// Whether a plan from this side reports its standing conditions (a
    /// foreign policy, an extra label, an unverifiable body): a live database
    /// does; from a declaration every difference is an op.
    pub(crate) fn reports_conditions(&self) -> bool {
        matches!(self.0, SideKind::Live { .. })
    }

    /// The foreign default of `column` on `model` as the side holds it, read
    /// for a plan ([`foreign_default`]): a live column's server default ferro
    /// did not leave, as the catalog prints it (a literal's display cast
    /// folded). A declaration holds none: its `default` is the field's
    /// Python default, which ferro never persists (ADR-0027).
    pub(crate) fn foreign_default(
        &self,
        model: &SchemaModel,
        column: &str,
        dialect: Dialect,
    ) -> Option<String> {
        if matches!(self.0, SideKind::Declared(_)) {
            return None;
        }
        let col = model.columns.iter().find(|col| col.name == column)?;
        foreign_default(col, dialect)
            .then(|| server_default_text(col).map(folded_default))
            .flatten()
    }

    /// `model` as the values a statement may write read it. A live column's
    /// `default` is the catalog's text of its server default, never a value
    /// a backfill can write, so a live side's is left out; a declaration's is
    /// its own.
    pub(crate) fn writable_values<'a>(&self, model: &'a SchemaModel) -> Cow<'a, SchemaModel> {
        match &self.0 {
            SideKind::Declared(_) => Cow::Borrowed(model),
            SideKind::Live { .. } => Cow::Owned(without_server_defaults(model)),
        }
    }

    /// The side's IR as [`Side::writable_values`] reads each of its models.
    pub(crate) fn writable_ir(&self) -> Cow<'_, IrEnvelope<SchemaIrPayload>> {
        match &self.0 {
            SideKind::Declared(ir) => Cow::Borrowed(ir),
            SideKind::Live { ir, .. } => {
                let mut ir = ir.clone();
                for model in &mut ir.payload.models {
                    *model = without_server_defaults(model);
                }
                Cow::Owned(ir)
            }
        }
    }

    /// Whether the side, as a plan's target, can express `op`. A declaration
    /// expresses every op. A live database cannot have an enum label removed
    /// (labels are append-only, ADR-0011, and rows may hold it), nor a row
    /// policy put back that applies `TO` a role list other than the default
    /// (ferro's `CREATE POLICY` never writes the clause): each refusal is the
    /// reason a revision's reader acts on.
    pub(crate) fn expresses(&self, op: &MigrationOp) -> Result<(), String> {
        if matches!(self.0, SideKind::Declared(_)) {
            return Ok(());
        }
        match op {
            MigrationOp::RemoveEnumLabel {
                type_name, label, ..
            } => Err(format!(
                "label {label:?} of enum type {type_name} cannot be removed: enum labels are \
                 append-only (ADR-0011), and rows may hold it"
            )),
            MigrationOp::DropDefault { table, column, was } => Err(format!(
                "the default {was} on {table}.{column} was set outside ferro, and no declared \
                 side holds it to put back; set it again by hand (ALTER TABLE \"{table}\" \
                 ALTER COLUMN \"{column}\" SET DEFAULT {was}) if it is wanted"
            )),
            MigrationOp::AddRowPolicy { table, name }
            | MigrationOp::RebuildRowPolicy { table, name } => match self.policy(table, name) {
                Some(body) if !ferro_ddl_lowering::is_default_row_policy_roles(&body.roles) => {
                    Err(format!(
                        "row policy {name} on {table} applies TO {}, a clause ferro's CREATE \
                         POLICY never writes; restore it by hand",
                        body.roles.join(", ")
                    ))
                }
                _ => Ok(()),
            },
            _ => Ok(()),
        }
    }

    /// The side as the rename `ops` (planned from it under `hints`) leave
    /// it: the planned-before side every other op of the plan reads.
    fn renamed(&self, ops: &[MigrationOp], hints: &[Hint], new: &Side, dialect: Dialect) -> Side {
        let ir = before_renamed_by(self.ir(), hints, dialect);
        match &self.0 {
            SideKind::Declared(_) => Side::declared(ir),
            SideKind::Live { facts, .. } => Side(SideKind::Live {
                ir,
                facts: renamed_facts(facts, ops, new.ir(), dialect),
            }),
        }
    }

    /// The renames of the names only a live database's facts carry — a live
    /// IR holds no check and no row policy ([`fact_renames`]). A declaration
    /// carries every name in its IR, so it adds none.
    fn fact_renames(&self, ops: &mut Vec<MigrationOp>, hints: &[Hint], dialect: Dialect) {
        if let SideKind::Live { ir, facts } = &self.0 {
            fact_renames(ops, facts, ir, hints, dialect);
        }
    }
}

// -- foreign defaults (ADR-0027, ADR-0034 as amended) ---------------------------------

/// Whether the live column `col` holds a *foreign default*: a server default
/// ferro did not leave there. Ferro persists no default of its own
/// (ADR-0027) but two:
///
/// | Dialect | Ferro's own | Foreign |
/// | :-- | :-- | :-- |
/// | Postgres | the default of a column that owns a sequence (`serial`, read as `autoincrement`), whatever the sequence is called | every other default |
/// | SQLite | a literal on a `NOT NULL` column (ADR-0034's kept default), whatever the literal | an expression, or any default on a nullable column |
///
/// The sequence is recognised by the column's ownership of it
/// (`pg_get_serial_sequence`, read by introspection), never by the text of
/// its `nextval`: a renamed table's sequence is still its own. SQLite's rule
/// is by shape alone: a hand-set literal on a `NOT NULL` column cannot be
/// told from the pass's and is accepted too (ADR-0034 as amended).
///
/// The question is asked of a live column only ([`Side::foreign_default`]):
/// a declared column's `default` is its Python default.
pub fn foreign_default(col: &ferro_schema_ir::SchemaColumn, dialect: Dialect) -> bool {
    let Some(text) = server_default_text(col) else {
        return false;
    };
    match dialect {
        Dialect::Postgres => !col.autoincrement,
        Dialect::Sqlite => col.nullable || !is_sqlite_literal(text),
    }
}

/// A live column's server default, as introspection read it.
fn server_default_text(col: &ferro_schema_ir::SchemaColumn) -> Option<&str> {
    col.default.as_ref()?.as_str()
}

/// `model` with no column default: what a live table's columns give a
/// statement to write ([`Side::writable_values`]).
fn without_server_defaults(model: &SchemaModel) -> SchemaModel {
    let mut model = model.clone();
    for col in &mut model.columns {
        col.default = None;
    }
    model
}

/// The byte just past the quoted SQL string literal `text` opens with
/// (`'it''s'` is one literal), or `None` when it opens with none.
fn quoted_literal_end(text: &str) -> Option<usize> {
    let bytes = text.as_bytes();
    if bytes.first() != Some(&b'\'') {
        return None;
    }
    let mut at = 1;
    while at < bytes.len() {
        if bytes[at] == b'\'' {
            if bytes.get(at + 1) == Some(&b'\'') {
                at += 2;
                continue;
            }
            return Some(at + 1);
        }
        at += 1;
    }
    None
}

/// Whether SQLite's `dflt_value` text is a literal: a string, a blob, a
/// number, `NULL`, `TRUE` or `FALSE`. SQLite gives a parenthesised default
/// back without its parentheses, so `DEFAULT (1)` is the literal `1` and
/// `DEFAULT (datetime('now'))` the expression `datetime('now')`.
fn is_sqlite_literal(text: &str) -> bool {
    let text = text.trim();
    if quoted_literal_end(text) == Some(text.len()) {
        return true;
    }
    if let Some(blob) = text.strip_prefix(['x', 'X'])
        && quoted_literal_end(blob) == Some(blob.len())
    {
        return true;
    }
    if ["NULL", "TRUE", "FALSE"]
        .iter()
        .any(|word| text.eq_ignore_ascii_case(word))
    {
        return true;
    }
    let unsigned = text.strip_prefix(['+', '-']).unwrap_or(text);
    if let Some(hex) = unsigned
        .strip_prefix("0x")
        .or_else(|| unsigned.strip_prefix("0X"))
    {
        return !hex.is_empty() && hex.chars().all(|c| c.is_ascii_hexdigit());
    }
    unsigned
        .chars()
        .next()
        .is_some_and(|c| c.is_ascii_digit() || c == '.')
        && unsigned.parse::<f64>().is_ok()
}

/// A server default as a person reads it: the catalog's text with the cast
/// Postgres adds to a literal for display folded (`''::character varying`
/// is `''`, `'free'::text` is `'free'`); any other text as it is.
fn folded_default(text: &str) -> String {
    let text = text.trim();
    if let Some(end) = quoted_literal_end(text)
        && let Some(cast) = text[end..].strip_prefix("::")
        && !cast.is_empty()
        && cast
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || " _()[],\"".contains(c))
    {
        return text[..end].to_string();
    }
    text.to_string()
}

/// The foreign default of every column `old_model` and `new_model` share
/// ([`Side::foreign_default`]). Read from a live `old` (drift, baseline, the
/// pass, a revision's upgrade): dropped under `destructive`, else reported
/// and left. Read from a live `new` (a revision's downgrade): the op whose
/// verdict says no declared side can put the default back.
#[allow(clippy::too_many_arguments)]
fn plan_foreign_defaults(
    old: &Side,
    new: &Side,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    dialect: Dialect,
    destructive: bool,
    ops: &mut Vec<MigrationOp>,
    reports: &mut Vec<Report>,
) {
    let table = new_model.table_name.as_str();
    for col in &new_model.columns {
        if !old_model.columns.iter().any(|old| old.name == col.name) {
            continue;
        }
        let (was, from_live) = match (
            old.foreign_default(old_model, &col.name, dialect),
            new.foreign_default(new_model, &col.name, dialect),
        ) {
            (Some(was), _) => (was, true),
            (None, Some(was)) => (was, false),
            (None, None) => continue,
        };
        if destructive {
            ops.push(MigrationOp::DropDefault {
                table: table.to_string(),
                column: col.name.clone(),
                was,
            });
        } else if from_live {
            reports.push(foreign_default_warning(table, &col.name, &was, dialect));
        }
    }
}

/// A live policy as a declaration of it: raw, its bodies as the catalog
/// printed them, and the roles it applies `TO`. `None` for a command the
/// vocabulary does not know.
fn live_policy_body(policy: &LiveRowPolicy) -> Option<PolicyBody> {
    let command = serde_json::from_value(serde_json::Value::String(policy.command.clone())).ok()?;
    Some(PolicyBody {
        policy: ferro_schema_ir::SchemaRowPolicy {
            name: policy.name.clone(),
            command,
            restrictive: policy.restrictive,
            expr: ferro_schema_ir::RowPolicyExpr::Raw {
                using: policy.using.clone(),
                with_check: policy.with_check.clone(),
            },
        },
        roles: policy.roles.clone(),
    })
}

/// `LiveRowSecurity` lives in `ferro_ddl_lowering` with `Deserialize` only;
/// its wire shape is that same field layout.
fn serialize_row_security<S: serde::Serializer>(
    live: &LiveRowSecurity,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    use serde::Serialize;
    let policies: Vec<serde_json::Value> = live
        .policies
        .iter()
        .map(|policy| {
            serde_json::json!({
                "name": policy.name,
                "command": policy.command,
                "restrictive": policy.restrictive,
                "using": policy.using,
                "with_check": policy.with_check,
                "roles": policy.roles,
                "ferro_owned": policy.ferro_owned,
            })
        })
        .collect();
    serde_json::json!({
        "enabled": live.enabled,
        "forced": live.forced,
        "policies": policies,
    })
    .serialize(serializer)
}

/// The facts a table declared in `old` would show live: every declared CHECK
/// ferro-owned and valid; every FK and index valid; row security on exactly
/// as declared, each policy's bodies as ferro renders them. A declaration
/// has no catalog, so its checks carry no `definition`: a declared check's
/// body is rendered for the dialect where it is compared ([`check_differs`]).
fn declared_table_facts(old_model: &SchemaModel) -> LiveTableFacts {
    let checks = old_model
        .table_checks
        .iter()
        .map(|check| (check.name.clone(), None))
        .chain(
            old_model
                .checks
                .iter()
                .map(|check| (check.name.clone(), Some(check.column.clone()))),
        )
        .map(|(name, column)| LiveCheckFact {
            name,
            ferro_owned: true,
            validated: true,
            column,
            ..LiveCheckFact::default()
        })
        .collect();
    LiveTableFacts {
        checks,
        foreign_keys: Vec::new(),
        indexes: Vec::new(),
        row_security: declared_row_security(old_model),
        generated: Vec::new(),
    }
}

/// A declared table as introspection would report it: on Postgres, a column
/// whose storage resolves to a native enum is read back as one
/// (`postgres_native_enum`), the flag the storage-drift decision reads on its
/// live side. Without it two identical declared enum columns would read as
/// drift.
fn declared_live_view(model: &SchemaModel, dialect: Dialect) -> Cow<'_, SchemaModel> {
    if dialect != Dialect::Postgres {
        return Cow::Borrowed(model);
    }
    let native_enum = |col: &ferro_schema_ir::SchemaColumn| {
        matches!(
            resolve_column_storage(col, dialect),
            Ok(ResolvedStorage::PgEnum { .. })
        )
    };
    if !model
        .columns
        .iter()
        .any(|col| native_enum(col) && !col.postgres_native_enum)
    {
        return Cow::Borrowed(model);
    }
    let mut view = model.clone();
    for col in &mut view.columns {
        if native_enum(col) {
            col.postgres_native_enum = true;
        }
    }
    Cow::Owned(view)
}

fn declared_row_security(model: &SchemaModel) -> LiveRowSecurity {
    let Some(declaration) = model.row_security.as_ref() else {
        return LiveRowSecurity::default();
    };
    LiveRowSecurity {
        enabled: true,
        forced: declaration.force,
        policies: declaration
            .policies
            .iter()
            .map(|policy| {
                // A policy whose clauses cannot render is invalid IR, which
                // `Plan::render` rejects before any statement of this plan can
                // run (`validate_schema_ir`).
                let (using, with_check) = row_policy_clauses(model, policy).unwrap_or_default();
                LiveRowPolicy {
                    name: policy.name.clone(),
                    command: row_policy_command_token(policy.command).to_string(),
                    restrictive: policy.restrictive,
                    using,
                    with_check,
                    roles: Vec::new(),
                    ferro_owned: is_ferro_row_policy_name(&policy.name),
                }
            })
            .collect(),
    }
}

/// Decide every change that turns `old` into `new`, for the whole modelset.
///
/// Each side is an adapter ([`Side::declared`], [`Side::live`]); the planner
/// asks it questions and never which one it holds (ADR-0050). `options`
/// gates the ops that remove something (ADR-0013's ladder): without
/// `destructive`, drops are left out and their leftover reports stand.
///
/// The plan is in execution order:
///
/// 1. On Postgres, label additions to existing enum types (ADR-0011) — first,
///    so any later statement may name a new label — then the creation of
///    every enum type the plan introduces and that does not yet exist.
/// 2. New tables, parents before children, each followed by what its
///    `CREATE TABLE` cannot carry from the side it is read from (a live
///    table's checks, row-security flags and policies, which its IR does not
///    hold).
/// 3. Every table in both snapshots, ordered by model name and then so a
///    table follows the tables its foreign keys reference, each table's ops
///    in the reconciliation pass's order: column adds and alters; index adds
///    (and, destructive, drops); invalid-index rebuilds; FK adds and
///    rebuilds; CHECK adds, rebuilds, validations of `NOT VALID`
///    constraints, (destructive) CHECK drops; row security last — flags,
///    policy adds and rebuilds, (destructive) orphan drops and teardown —
///    after the table's column and data steps; then (destructive) its
///    column drops.
/// 4. (Destructive) dropped tables, children before parents, then the enum
///    types nothing declares any more.
///
/// Ahead of all of it — so a new table can reference a renamed one — the
/// renames `new`'s live rename hints declare ([`live_hints`], ADR-0032), one
/// unit per table ([`rename_ops`]): its table and column renames, then every
/// derived index, constraint and (Postgres) policy name, the renamed tables
/// before the tables that reference them. Everything after is planned from
/// `old` as the renames leave it, so a rename and a type change on one column
/// are the rename, then the type change. A live IR declares no hint, so the
/// plan from a declared `old` that already holds the new names, and every
/// plan over a modelset without hints, is unchanged by them. A refused hint
/// ([`HintError`]) applies no rename and stands in
/// [`Plan::reports`] as [`crate::ReportKind::HintRefused`]; the generator
/// refuses it before writing anything.
pub fn plan_from_ir(old: &Side, new: &Side, dialect: Dialect, options: PlanOptions) -> Plan {
    match live_hints(&old.ir().payload, &new.ir().payload) {
        Ok(hints) => plan_with_hints(old, new, &hints, dialect, options),
        Err(refusal) => {
            let mut draft = plan_named(old, new, dialect, options);
            draft.reports.push(hint_refusal_warning(&refusal));
            Plan::decided(old.clone(), new.clone(), dialect, draft)
        }
    }
}

/// [`plan_from_ir`] with the renames `hints` declare: their rename ops
/// first, then everything else planned from `old` as they leave it.
fn plan_with_hints(
    old: &Side,
    new: &Side,
    hints: &[Hint],
    dialect: Dialect,
    options: PlanOptions,
) -> Plan {
    if hints.is_empty() {
        let draft = plan_named(old, new, dialect, options);
        return Plan::decided(old.clone(), new.clone(), dialect, draft);
    }
    let renamed = renamed_snapshot(old.ir(), hints);
    let mut operations = rename_ops(old.ir(), &renamed, dialect);
    old.fact_renames(&mut operations, hints, dialect);
    let before = old.renamed(&operations, hints, new, dialect);
    let mut draft = plan_named(&before, new, dialect, options);
    operations.append(&mut draft.operations);
    draft.operations = operations;
    Plan::decided(before, new.clone(), dialect, draft)
}

/// What planning decides before each op gets its verdict: the ops in
/// execution order and the reports raised beside them.
#[derive(Default)]
pub(crate) struct Draft {
    /// The ops, in execution order.
    pub(crate) operations: Vec<MigrationOp>,
    /// The reports, in the order planning raised them.
    pub(crate) reports: Vec<Report>,
}

/// `old` as the renames [`plan_from_ir`]`(old, new, dialect, …)` plans leave
/// it: the side every op of that plan but the renames was decided against,
/// which the [`Plan`] holds and renders against. The generator's own renders
/// between stages ([`crate::render::render_ops`]) read it too. A
/// type change of a renamed column names the column by its new name, which
/// only this side holds.
///
/// Borrowed when `new` declares no live hint (or a refused one, which
/// renames nothing), so a side that already holds the new names — a
/// generator's renamed parent — is its own.
pub(crate) fn planned_before<'a>(
    old: &'a IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Cow<'a, IrEnvelope<SchemaIrPayload>> {
    match live_hints(&old.payload, &new.payload) {
        Ok(hints) if !hints.is_empty() => Cow::Owned(before_renamed_by(old, &hints, dialect)),
        _ => Cow::Borrowed(old),
    }
}

/// `old` as the live `hints` leave it on `dialect`: [`planned_before`]'s one
/// derivation ([`storage_hints`] keeps a SQLite label hint out, the rows'
/// update is not a rename of the schema).
fn before_renamed_by(
    old: &IrEnvelope<SchemaIrPayload>,
    hints: &[Hint],
    dialect: Dialect,
) -> IrEnvelope<SchemaIrPayload> {
    renamed_snapshot(old, &storage_hints(hints, dialect))
}

/// The renames of the names only a live database's [`LiveFacts`] carry — a
/// live IR holds no check and no row policy — added to `ops`, each at the end
/// of its table's unit so the table's renames stay one contiguous unit. Each
/// ferro-owned check and `rls_` policy name is re-derived by the function
/// that made it (a column check through `db_check_constraint_name`, a table
/// check and a policy through their suffix), exactly as [`renamed_snapshot`]
/// re-derives a declared one; a name an IR rename op already covers is left
/// alone.
fn fact_renames(
    ops: &mut Vec<MigrationOp>,
    facts: &LiveFacts,
    old: &IrEnvelope<SchemaIrPayload>,
    hints: &[Hint],
    dialect: Dialect,
) {
    use ferro_ddl_lowering::{
        db_check_constraint_name, row_policy_name, row_policy_short_name,
        table_check_constraint_name, table_check_suffix,
    };
    let renames = Renames { hints };
    let old_models = index_models(&old.payload.models);
    for (t_old, live) in &facts.tables {
        let Some(model) = old_models.get(t_old) else {
            continue;
        };
        let t_new = renames.table(t_old).to_string();
        let mut added = Vec::new();
        for check in live.checks.iter().filter(|check| check.ferro_owned) {
            let by_column = model.columns.iter().find_map(|col| {
                (check.name == db_check_constraint_name(t_old, &col.name))
                    .then(|| db_check_constraint_name(&t_new, renames.column(t_old, &col.name)))
            });
            let renamed = by_column.or_else(|| {
                table_check_suffix(t_old, &check.name)
                    .map(|suffix| table_check_constraint_name(&t_new, suffix))
            });
            if let Some(name) = renamed.filter(|name| *name != check.name) {
                added.push(MigrationOp::RenameConstraint {
                    table: t_new.clone(),
                    old: check.name.clone(),
                    new: name,
                });
            }
        }
        if dialect == Dialect::Postgres {
            for policy in live.row_security.policies.iter().filter(|p| p.ferro_owned) {
                if let Some(short) = row_policy_short_name(t_old, &policy.name) {
                    let name = row_policy_name(&t_new, short);
                    if name != policy.name {
                        added.push(MigrationOp::RenamePolicy {
                            table: t_new.clone(),
                            old: policy.name.clone(),
                            new: name,
                        });
                    }
                }
            }
        }
        added.retain(|op| !ops.contains(op));
        if added.is_empty() {
            continue;
        }
        let at = ops
            .iter()
            .rposition(|op| op.table() == Some(t_new.as_str()))
            .map_or(ops.len(), |last| last + 1);
        ops.splice(at..at, added);
    }
}

/// `facts` as the rename `ops` leave the live database: each table under its
/// new name, every renamed check, foreign key, index and policy under its new
/// name. A rename carries every column reference with it (Postgres and SQLite
/// both rewrite the bodies that name a renamed column), so a check or
/// shorthand policy whose live body equals its declaration rendered under the
/// old names (the one normalizer each: `normalize_check_definition`,
/// ADR-0015; `normalize_row_policy_expr`) reads as the declaration under the
/// new names. Any other live body is kept as read, so the drift decision
/// rebuilds it in this same plan. A rename the dialect
/// cannot run in place renames no fact: on SQLite a constraint keeps its live
/// name until a generated migration's rebuild renames it, so the plan reports
/// the catalog as it is.
fn renamed_facts(
    facts: &LiveFacts,
    ops: &[MigrationOp],
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> LiveFacts {
    if (facts.tables.is_empty() && facts.enum_labels.is_empty()) || ops.is_empty() {
        return facts.clone();
    }
    let new_models = index_models(&new.payload.models);
    let mut out = facts.clone();
    for op in ops {
        match op {
            MigrationOp::RenameTable { old, new } => {
                if let Some(table) = out.tables.remove(old) {
                    out.tables.insert(new.clone(), table);
                }
            }
            // A native type renames in place (its label list moves with it),
            // and a label rename relabels the type's one entry: the rows
            // follow, so the type holds the new spelling and no longer the
            // old. A label stored as text renames no type and is no fact.
            MigrationOp::RenameEnumType { old, new } => {
                if let Some(labels) = out.enum_labels.remove(old) {
                    out.enum_labels.insert(new.clone(), labels);
                }
            }
            MigrationOp::RenameEnumLabel {
                type_name,
                old,
                new,
                ..
            } => {
                if let Some(label) = out
                    .enum_labels
                    .get_mut(type_name)
                    .and_then(|labels| labels.iter_mut().find(|label| *label == old))
                {
                    label.clone_from(new);
                }
            }
            _ => {}
        }
    }
    let touched: BTreeSet<&str> = ops.iter().filter_map(MigrationOp::table).collect();
    // The renames run backwards, from the ops themselves: what turns the new
    // names back into the ones the live facts were read under.
    let old_table = |table: &str| {
        ops.iter()
            .find_map(|op| match op {
                MigrationOp::RenameTable { old, new } if new == table => Some(old.clone()),
                _ => None,
            })
            .unwrap_or_else(|| table.to_string())
    };
    let reverse: Vec<Hint> = ops
        .iter()
        .filter_map(|op| match op {
            MigrationOp::RenameTable { old, new } => Some(Hint::Table {
                old: new.clone(),
                new: old.clone(),
            }),
            MigrationOp::RenameColumn { table, old, new } => Some(Hint::Column {
                table: old_table(table),
                old: new.clone(),
                new: old.clone(),
            }),
            _ => None,
        })
        .collect();
    for (table, live) in &mut out.tables {
        if !touched.contains(table.as_str()) {
            continue;
        }
        let renamed_name = |name: &str| {
            ops.iter()
                .find_map(|op| match op {
                    MigrationOp::RenameConstraint { .. } if dialect == Dialect::Sqlite => None,
                    MigrationOp::RenameIndex { table: t, old, new }
                    | MigrationOp::RenameConstraint { table: t, old, new }
                    | MigrationOp::RenamePolicy { table: t, old, new }
                        if t == table && old == name =>
                    {
                        Some(new.clone())
                    }
                    _ => None,
                })
                .unwrap_or_else(|| name.to_string())
        };
        let declared = new_models.get(table.as_str()).copied();
        // The declaration rendered under the names the live bodies were read
        // with, before the renames: the side a live body is compared with.
        let before = declared.map(|model| renamed_model(model, &Renames { hints: &reverse }));
        for check in &mut live.checks {
            check.name = renamed_name(&check.name);
            let (Some(model), Some(before)) = (declared, before.as_ref()) else {
                continue;
            };
            let Some(index) = declared_check_names(model)
                .iter()
                .position(|name| *name == check.name)
            else {
                continue;
            };
            // A body that does not render is kept as read: the rebuild that
            // drift then plans fails rendering it, naming the check.
            let Ok(Some(now)) = declared_check_body(model, &check.name, dialect) else {
                continue;
            };
            // Whether the live body is the declaration under the old names:
            // on Postgres as Postgres printed both (ADR-0060), on SQLite by
            // the one normalizer (ADR-0015).
            let follows = match dialect {
                Dialect::Postgres => check
                    .printed
                    .as_ref()
                    .is_some_and(|pair| pair.declared == pair.catalog),
                Dialect::Sqlite => declared_check_names(before)
                    .get(index)
                    .and_then(|was| declared_check_body(before, was, dialect).ok().flatten())
                    .is_some_and(|was| {
                        normalize_check_definition(&was)
                            == normalize_check_definition(&check.definition)
                    }),
            };
            if follows {
                check.definition = format!("CHECK ({now})");
            }
        }
        for generated in &mut live.generated {
            if let Some(new) = ops.iter().find_map(|op| match op {
                MigrationOp::RenameColumn { table: t, old, new }
                    if t == table && *old == generated.column =>
                {
                    Some(new.clone())
                }
                _ => None,
            }) {
                generated.column = new;
            }
            // SQLite rewrites a stored expression's column names on a rename,
            // as Postgres does; only SQLite's catalog is compared as text.
            if dialect != Dialect::Sqlite {
                continue;
            }
            let (Some(model), Some(before)) = (declared, before.as_ref()) else {
                continue;
            };
            let Some(i) = model
                .columns
                .iter()
                .position(|col| col.name == generated.column)
            else {
                continue;
            };
            let text = |model: &SchemaModel, col| {
                emit::generated_column_text(model, col, Dialect::Sqlite)
                    .ok()
                    .flatten()
            };
            if let (Some(was), Some(now)) = (
                text(before, &before.columns[i]),
                text(model, &model.columns[i]),
            ) && fold_whitespace(&was) == fold_whitespace(&generated.catalog)
            {
                generated.catalog = now;
            }
        }
        for fk in &mut live.foreign_keys {
            fk.name = renamed_name(&fk.name);
        }
        for index in &mut live.indexes {
            index.name = renamed_name(&index.name);
        }
        for policy in &mut live.row_security.policies {
            policy.name = renamed_name(&policy.name);
            let clauses = declared.zip(before.as_ref()).and_then(|(model, before)| {
                let i = model
                    .row_security
                    .as_ref()?
                    .policies
                    .iter()
                    .position(|p| p.name == policy.name)?;
                let now_ir = &model.row_security.as_ref()?.policies[i];
                let was_ir = &before.row_security.as_ref()?.policies[i];
                if !matches!(now_ir.expr, ferro_schema_ir::RowPolicyExpr::Setting { .. }) {
                    return None;
                }
                Some((
                    row_policy_clauses(before, was_ir).ok()?,
                    row_policy_clauses(model, now_ir).ok()?,
                ))
            });
            let normalized = |expr: &Option<String>| expr.as_deref().map(normalize_row_policy_expr);
            if let Some(((was_using, was_check), (now_using, now_check))) = clauses
                && normalized(&was_using) == normalized(&policy.using)
                && normalized(&was_check) == normalized(&policy.with_check)
            {
                policy.using = now_using;
                policy.with_check = now_check;
            }
        }
    }
    out
}

/// Every change [`plan_from_ir`] plans once the tables and columns of `old`
/// and `new` are matched by name: `old` is the planned-before side the plan
/// holds.
fn plan_named(old: &Side, new: &Side, dialect: Dialect, options: PlanOptions) -> Draft {
    let old_models = index_models(&old.ir().payload.models);
    let new_models = index_models(&new.ir().payload.models);
    let mut plan = Draft::default();

    let added: Vec<&SchemaModel> = new_models
        .iter()
        .filter(|(table, _)| !old_models.contains_key(*table))
        .map(|(_, model)| *model)
        .collect();
    let dropped: Vec<&SchemaModel> = old_models
        .iter()
        .filter(|(table, _)| !new_models.contains_key(*table))
        .map(|(_, model)| *model)
        .collect();

    if dialect == Dialect::Postgres {
        plan_enum_label_additions(old, new, &mut plan);
        plan_enum_type_creation(old, new, &old_models, &mut plan);
    }
    if old.plans_label_removals() {
        plan_enum_label_removals(old.ir(), new.ir(), &mut plan);
    }

    for model in emit::order_models_for_create(&added) {
        plan.operations.push(MigrationOp::AddTable {
            table: model.table_name.clone(),
        });
        plan.operations
            .extend(beyond_the_create(new, model, dialect));
    }

    for (old_model, new_model) in order_existing_tables(&old_models, &new_models) {
        plan_existing_table(old, new, old_model, new_model, dialect, options, &mut plan);
    }

    if options.destructive {
        for model in emit::order_models_for_create(&dropped).into_iter().rev() {
            plan.operations.push(MigrationOp::DropTable {
                table: model.table_name.clone(),
            });
        }
        if dialect == Dialect::Postgres {
            plan_enum_type_drops(old.ir(), new.ir(), &old_models, &new_models, &mut plan);
        }
    }

    plan
}

/// What a table `side` holds needs beyond its `CREATE TABLE`, which renders
/// the table's IR: each check, row-security flag and policy its facts hold
/// that its IR does not declare. A declaration's facts are its IR, so it
/// needs nothing more; a live table's checks and row security are facts
/// only, put back after the table as the catalog printed them.
fn beyond_the_create(side: &Side, model: &SchemaModel, dialect: Dialect) -> Vec<MigrationOp> {
    let table = model.table_name.as_str();
    let facts = side.table_facts(model);
    let declared_checks = declared_check_names(model);
    let mut ops: Vec<MigrationOp> = facts
        .checks
        .iter()
        .filter(|check| !declared_checks.contains(&check.name))
        .map(|check| MigrationOp::AddCheck {
            table: table.to_string(),
            name: check.name.clone(),
        })
        .collect();
    if dialect != Dialect::Postgres {
        return ops;
    }
    let declared = model.row_security.as_ref();
    let live = &facts.row_security;
    if live.enabled && declared.is_none() {
        ops.push(flag_op(table, RowSecurityFlag::Enable));
    }
    if live.forced && !declared.is_some_and(|declaration| declaration.force) {
        ops.push(flag_op(table, RowSecurityFlag::Force));
    }
    let declared_policies = declared_row_policy_names(model);
    ops.extend(
        live.policies
            .iter()
            .filter(|policy| !declared_policies.contains(&policy.name))
            .map(|policy| MigrationOp::AddRowPolicy {
                table: table.to_string(),
                name: policy.name.clone(),
            }),
    );
    ops
}

/// Tables present in both snapshots, sorted by model name and then so each
/// follows the tables its foreign keys reference — the order the
/// reconciliation pass has always visited them in (#302).
fn order_existing_tables<'a>(
    old_models: &BTreeMap<String, &'a SchemaModel>,
    new_models: &BTreeMap<String, &'a SchemaModel>,
) -> Vec<(&'a SchemaModel, &'a SchemaModel)> {
    let mut existing: Vec<(&SchemaModel, &SchemaModel)> = new_models
        .iter()
        .filter_map(|(table, new_model)| Some((*old_models.get(table)?, *new_model)))
        .collect();
    existing.sort_by(|a, b| a.1.model_name.cmp(&b.1.model_name));
    crate::order_by_dependencies(
        existing,
        |(_, new_model)| new_model.table_name.clone(),
        |(_, new_model)| {
            new_model
                .foreign_keys
                .iter()
                .map(|fk| fk.to_table.clone())
                .collect()
        },
    )
}

fn plan_existing_table(
    old: &Side,
    new: &Side,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    dialect: Dialect,
    options: PlanOptions,
    plan: &mut Draft,
) {
    let table = new_model.table_name.as_str();
    let facts = old.table_facts(old_model);
    let old_view = old.table_view(old_model, dialect);
    // A generated column dropped and added back is diffed as the table holds
    // it after the drop: absent, so the add brings it back with what rides it.
    let replaced = replaced_generated_columns(
        old,
        new,
        old_view.as_ref(),
        new_model,
        dialect,
        options.destructive,
    );
    // A plain column the model makes generated, which only a plan that
    // drops data replaces: left, and said so.
    if !options.destructive {
        plan.reports.extend(
            new_model
                .columns
                .iter()
                .filter(|col| {
                    new.holds_generated(new_model, &col.name)
                        && old_view.columns.iter().any(|was| was.name == col.name)
                        && !old.holds_generated(old_view.as_ref(), &col.name)
                })
                .map(|col| {
                    ferro_ddl_lowering::plain_column_kept_warning(table, &col.name, dialect)
                }),
        );
    }
    let old_view = if replaced.is_empty() {
        old_view
    } else {
        Cow::Owned(without_columns(old_view.as_ref(), &replaced))
    };
    let old_model = old_view.as_ref();
    let mut ops = Vec::new();
    let mut column_drops = Vec::new();

    ops.extend(primary_key_change(table, old_model, new_model));
    // A foreign default goes first among the column ops: a type change after
    // it then never has a default to cast.
    plan_foreign_defaults(
        old,
        new,
        old_model,
        new_model,
        dialect,
        options.destructive,
        &mut ops,
        &mut plan.reports,
    );
    // Postgres refuses to drop a column a policy reads: a policy the plan
    // can carry ([`carries_policy`]) is dropped first and added back with the
    // table's row security, after the add. Any other policy that reads a
    // replaced column refuses its drop ([`policy_blocking_replace`]).
    let declaration = new.declaration(new_model);
    let carried: Vec<String> = if dialect == Dialect::Postgres {
        facts
            .row_security
            .policies
            .iter()
            .filter(|policy| {
                replaced
                    .iter()
                    .any(|column| carries_policy(old, new, &declaration, policy, column))
            })
            .map(|policy| policy.name.clone())
            .collect()
    } else {
        Vec::new()
    };
    ops.extend(carried.iter().map(|name| MigrationOp::DropRowPolicy {
        table: table.to_string(),
        name: name.clone(),
    }));
    ops.extend(replaced.iter().map(|column| MigrationOp::DropColumn {
        table: table.to_string(),
        column: column.clone(),
    }));
    let mut column_ops = Vec::new();
    diff_model_columns(
        table,
        old_model,
        new_model,
        dialect,
        &mut column_ops,
        &mut column_drops,
    );
    // Every generated column is added after every other column op: after
    // the inputs its expression reads are added, and a replaced one after
    // the type change of an input it reads (Postgres's expression reads no
    // other generated column).
    let (added_back, column_ops): (Vec<MigrationOp>, Vec<MigrationOp>) =
        column_ops.into_iter().partition(|op| {
            matches!(op, MigrationOp::AddColumn { column, .. }
                if new.holds_generated(new_model, column))
        });
    ops.extend(column_ops);
    ops.extend(added_back);
    diff_generated(old, new, table, old_model, new_model, dialect, &mut ops);
    diff_model_indexes(table, old_model, new_model, options.destructive, &mut ops);
    // Invalid-index rebuilds (#515; ADR-0044) go where `AddIndex` goes: after
    // the column ops, ahead of the foreign-key ops. An invalid index is
    // present live, so the diff above never planned an `AddIndex` for it.
    // A redefinition drops the index and builds it anew, so it stands in for
    // the rebuild of an invalid one.
    let redefined: Vec<String> = ops
        .iter()
        .filter_map(|op| match op {
            MigrationOp::RedefineIndex { name, .. } => Some(name.clone()),
            _ => None,
        })
        .collect();
    ops.extend(
        index_rebuilds(table, new_model, &facts.indexes)
            .into_iter()
            .filter(|op| {
                !matches!(op, MigrationOp::RebuildIndex { name, .. } if redefined.contains(name))
            }),
    );
    diff_model_foreign_keys(
        table,
        old_model,
        new_model,
        options.destructive,
        &mut ops,
        &mut plan.reports,
    );

    // The checks the target holds, in its order.
    let target_names: Vec<String> = new
        .checks(new_model)
        .into_iter()
        .map(|(name, _)| name)
        .collect();
    // Postgres drops every table check reading a column with the column: a
    // check over a replaced column is gone once its drop runs, so the plan
    // adds it back after the add (SQLite's rebuild writes the table's checks
    // into the table it builds).
    let taken: Vec<String> = if dialect == Dialect::Postgres {
        checks_reading(new_model, &replaced)
    } else {
        Vec::new()
    };
    let standing = |name: &String| !taken.contains(name);
    // Check addition (#343; ADR-0013) lands after the column ops, so a CHECK
    // over a newly added column follows its ADD COLUMN.
    let live_check_names: Vec<String> = facts
        .checks
        .iter()
        .map(|check| check.name.clone())
        .filter(standing)
        .collect();
    ops.extend(missing_checks(
        table,
        old_model,
        new_model,
        &target_names,
        &live_check_names,
    ));
    // Body drift (#344; ADR-0015, ADR-0060), after the adds. Only ferro-owned
    // names are eligible: a user-owned CHECK is never dropped.
    let rebuildable: Vec<&String> = target_names
        .iter()
        .filter(|name| {
            facts
                .checks
                .iter()
                .any(|check| check.name == **name && check.ferro_owned && standing(&check.name))
        })
        .collect();
    ops.extend(check_rebuilds(
        old,
        new,
        old_model,
        new_model,
        &rebuildable,
        dialect,
    ));
    // Validation (#515; ADR-0043): after every add, leaving out any name a
    // rebuild covers — the rebuild's bare ADD installs a valid constraint.
    let rebuilt: Vec<String> = ops
        .iter()
        .filter_map(|op| match op {
            MigrationOp::RebuildCheck { name, .. } => Some(name.clone()),
            MigrationOp::RebuildForeignKey { old_name, .. } => Some(old_name.clone()),
            _ => None,
        })
        .collect();
    let check_validity: Vec<LiveCheckValidity> = facts
        .checks
        .iter()
        .filter(|check| standing(&check.name))
        .map(|check| LiveCheckValidity {
            name: check.name.clone(),
            validated: check.validated,
        })
        .collect();
    ops.extend(
        validations(table, new_model, &facts.foreign_keys, &check_validity)
            .into_iter()
            .filter(|op| {
                !matches!(op, MigrationOp::ValidateConstraint { name, .. } if rebuilt.contains(name))
            }),
    );
    // Leftovers (#345; ADR-0013): always reported — a leftover CHECK keeps
    // rejecting rows the model now allows — and dropped only when destructive.
    // A dropped column's own check is no leftover: it goes with its column.
    let live_ferro_owned_names: Vec<String> = facts
        .checks
        .iter()
        .filter(|check| check.ferro_owned && standing(&check.name))
        .map(|check| check.name.clone())
        .collect();
    let extras = extra_check_names(&target_names, &live_ferro_owned_names);
    let riding: Vec<String> = if options.destructive {
        column_drops
            .iter()
            .filter_map(|op| match op {
                MigrationOp::DropColumn { column, .. } => old.column_check(old_model, column),
                _ => None,
            })
            .collect()
    } else {
        Vec::new()
    };
    let leftovers: Vec<String> = extras
        .iter()
        .filter(|name| !riding.contains(name))
        .cloned()
        .collect();
    plan.reports
        .extend(extra_check_names_warning(table, &leftovers));
    if options.destructive {
        ops.extend(extras.into_iter().map(|name| MigrationOp::DropCheck {
            table: table.to_string(),
            name,
        }));
    }

    // Row security lands last for the table (#413; PRD #406 user story 20):
    // every column change and data-shaped step above has run before any
    // policy starts filtering the rows it touches.
    let mut row_security = facts.row_security.clone();
    row_security
        .policies
        .retain(|policy| !carried.contains(&policy.name));
    plan_row_security(
        old,
        &declaration,
        &row_security,
        dialect,
        options.destructive,
        &mut ops,
        &mut plan.reports,
    );

    if options.destructive {
        // A generated column goes before the inputs its expression reads,
        // which Postgres refuses to drop while it reads them.
        let (generated, plain): (Vec<MigrationOp>, Vec<MigrationOp>) =
            column_drops.into_iter().partition(|op| {
                matches!(op, MigrationOp::DropColumn { column, .. }
                    if old.holds_generated(old_model, column))
            });
        ops.extend(generated);
        ops.extend(plain);
    }
    plan.operations.extend(ops);
}

/// Translate the row-security reconciliation decision for one table
/// (`plan_row_security_reconcile`, the single seam; AGENTS.md § I-1 item 16)
/// into ops, in its execution order: missing flags, policy additions,
/// rebuilds, then (destructive) orphan drops and the flag teardown. `model`
/// is the table as the target declares it ([`Side::declaration`]) and `live`
/// its row security on the `old` side.
///
/// The `old` side answers what a live table cannot (ADR-0019, ADR-0033):
/// planned from a declaration, a raw body that differs is the author's edit
/// ([`Side::rebuilds_unverifiable_bodies`]), so it is rebuilt like a
/// shorthand one; and row security the declaration declared was installed by
/// ferro ([`Side::proves_row_security_installed`]), so a declaration the
/// target drops is torn down whether or not a ferro-named policy is left to
/// witness it. Every difference is then an op, so the decision's reports of
/// live conditions (unverifiable and replaced bodies, a teardown done) are
/// carried only from a live side ([`Side::reports_conditions`]); a
/// non-destructive plan still reports the removals it withholds.
fn plan_row_security(
    old: &Side,
    model: &SchemaModel,
    live: &LiveRowSecurity,
    dialect: Dialect,
    destructive: bool,
    ops: &mut Vec<MigrationOp>,
    reports: &mut Vec<Report>,
) {
    // An Err is a declared policy whose clauses cannot render: invalid IR,
    // which `Plan::render` rejects (`validate_schema_ir`) before anything
    // executes.
    let Ok(decision) = plan_row_security_reconcile(model, live, dialect, destructive) else {
        return;
    };
    let table = model.table_name.as_str();
    if old.reports_conditions() {
        reports.extend(decision.reports);
    } else if !destructive {
        reports.extend(
            dropped_row_security_warning(model, live)
                .into_iter()
                .chain(extra_row_policy_names_warning(table, &decision.extra)),
        );
    }
    if dialect != Dialect::Postgres {
        return;
    }
    ops.extend(
        decision
            .missing_flags
            .iter()
            .map(|flag| flag_op(table, *flag)),
    );
    ops.extend(
        decision
            .missing
            .into_iter()
            .map(|name| MigrationOp::AddRowPolicy {
                table: table.to_string(),
                name,
            }),
    );
    // Rebuilds in declaration order: drifted bodies, and from a declaration
    // the edited raw ones.
    let rebuilt = |name: &String| {
        decision.drifted.contains(name)
            || (old.rebuilds_unverifiable_bodies() && decision.unverifiable.contains(name))
    };
    ops.extend(
        declared_row_policy_names(model)
            .into_iter()
            .filter(rebuilt)
            .map(|name| MigrationOp::RebuildRowPolicy {
                table: table.to_string(),
                name,
            }),
    );
    if destructive {
        ops.extend(
            decision
                .extra
                .into_iter()
                .map(|name| MigrationOp::DropRowPolicy {
                    table: table.to_string(),
                    name,
                }),
        );
        let installed_by_ferro =
            old.proves_row_security_installed() || ferro_manages_row_security(live);
        ops.extend(
            excess_row_security_flags(model, live, installed_by_ferro)
                .into_iter()
                .map(|flag| flag_op(table, flag)),
        );
    }
}

/// The op that sets one row-security flag on `table`.
fn flag_op(table: &str, flag: RowSecurityFlag) -> MigrationOp {
    let table = table.to_string();
    match flag {
        RowSecurityFlag::Enable => MigrationOp::EnableRowSecurity { table },
        RowSecurityFlag::Force => MigrationOp::ForceRowSecurity { table },
        RowSecurityFlag::NoForce => MigrationOp::NoForceRowSecurity { table },
        RowSecurityFlag::Disable => MigrationOp::DisableRowSecurity { table },
    }
}

/// Every enum the models declare, with the labels of its first declaring
/// column, every `(table, column)` that declares it, in model order, and its
/// label rename hints (the first declaring column that carries any).
/// [`declared_enum_types`] keeps the native Postgres types only (the types
/// that exist as database objects); [`declared_enum_labels`] every enum, how
/// ever it is stored (a `db_type="text"` column keeps its labels in its rows).
struct DeclaredEnumTypes {
    labels: BTreeMap<String, Vec<String>>,
    declaring: BTreeMap<String, Vec<(String, String)>>,
    renamed_labels: BTreeMap<String, ferro_schema_ir::SchemaRenamedLabels>,
}

/// The native enum type `col` is stored as, when it is one.
fn enum_type_of(col: &ferro_schema_ir::SchemaColumn) -> Option<(String, Vec<String>)> {
    match resolve_column_storage(col, Dialect::Postgres) {
        Ok(ResolvedStorage::PgEnum { type_name, labels }) => Some((type_name, labels)),
        _ => None,
    }
}

/// The enum `col` declares, how ever it is stored: the type name and labels
/// it would have as a native type (`resolve_column_storage` with the storage
/// override set aside, so the name and label spelling have one source).
pub(crate) fn enum_declaration(
    col: &ferro_schema_ir::SchemaColumn,
) -> Option<(String, Vec<String>)> {
    if col.db_type_explicit != Some(true) {
        return enum_type_of(col);
    }
    let declared = ferro_schema_ir::SchemaColumn {
        db_type: None,
        db_type_explicit: None,
        ..col.clone()
    };
    enum_type_of(&declared)
}

/// Whether a label rename rewrites `col`'s rows on `dialect`, rather than
/// renaming the label of a native type: the label is text in the rows (every
/// enum on SQLite, a `db_type="text"` enum on Postgres). Decided by the
/// column's storage (`resolve_column_storage`), the one decider.
pub(crate) fn relabels_rows(col: &ferro_schema_ir::SchemaColumn, dialect: Dialect) -> bool {
    !matches!(
        resolve_column_storage(col, dialect),
        Ok(ResolvedStorage::PgEnum { .. })
    )
}

/// How one column declares an enum: its type name and labels, or `None`.
type EnumOf = fn(&ferro_schema_ir::SchemaColumn) -> Option<(String, Vec<String>)>;

fn collect_enums(models: &[SchemaModel], of: EnumOf) -> DeclaredEnumTypes {
    let mut declared = DeclaredEnumTypes {
        labels: BTreeMap::new(),
        declaring: BTreeMap::new(),
        renamed_labels: BTreeMap::new(),
    };
    for model in models {
        for col in &model.columns {
            if let Some((type_name, labels)) = of(col) {
                declared
                    .declaring
                    .entry(type_name.clone())
                    .or_default()
                    .push((model.table_name.clone(), col.name.clone()));
                if let Some(hints) = &col.enum_renamed_labels {
                    declared
                        .renamed_labels
                        .entry(type_name.clone())
                        .or_insert_with(|| hints.clone());
                }
                declared.labels.entry(type_name).or_insert(labels);
            }
        }
    }
    declared
}

fn declared_enum_types(models: &[SchemaModel]) -> DeclaredEnumTypes {
    collect_enums(models, enum_type_of)
}

fn declared_enum_labels(models: &[SchemaModel]) -> DeclaredEnumTypes {
    collect_enums(models, enum_declaration)
}

/// Label addition (ADR-0011): for every type `new` declares that `old`
/// already holds, the labels it lacks; from a live database, a report naming
/// the live labels the model no longer declares (warn-never-act).
fn plan_enum_label_additions(old: &Side, new: &Side, plan: &mut Draft) {
    let declared = declared_enum_types(&new.ir().payload.models);
    for (type_name, labels) in &declared.labels {
        let Some(existing) = old.enum_labels(type_name) else {
            continue;
        };
        // From a declaration a dropped label is a removal
        // ([`plan_enum_label_removals`]); from a live database, it only warns.
        let extra = extra_enum_labels(labels, &existing);
        if old.reports_conditions()
            && let Some(report) = extra_enum_labels_warning(type_name, &extra)
        {
            plan.reports.push(report);
        }
        for label in missing_enum_labels(labels, &existing) {
            plan.operations.push(MigrationOp::AddEnumLabel {
                type_name: type_name.clone(),
                label,
            });
        }
    }
}

/// Label removal from a declared `old` (#536; never from a live database,
/// where ADR-0011 warns and never acts): every label an enum of `old`
/// declares that the same enum in `new` drops, how ever it is stored, unless
/// a declared hint renames it ([`enum_rename_ops`] owns that). One op per
/// label, over every column of `new` declaring the type. Planned on every
/// dialect: the backfill a removal asks for is the same everywhere
/// (ADR-0037); only Postgres's contract has a statement for it.
fn plan_enum_label_removals(
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    plan: &mut Draft,
) {
    let before = declared_enum_labels(&old.payload.models);
    let after = declared_enum_labels(&new.payload.models);
    for (type_name, labels) in &after.labels {
        let Some(old_labels) = before.labels.get(type_name) else {
            continue;
        };
        let renamed_away: Vec<&String> = after
            .renamed_labels
            .get(type_name)
            .map(|hints| hints.labels.values().collect())
            .unwrap_or_default();
        for label in extra_enum_labels(labels, old_labels) {
            if renamed_away.contains(&&label) {
                continue;
            }
            plan.operations.push(MigrationOp::RemoveEnumLabel {
                type_name: type_name.clone(),
                label,
                columns: after.declaring.get(type_name).cloned().unwrap_or_default(),
            });
        }
    }
}

/// Every `(type, label)` an enum `new` declares adds to the same enum `old`
/// declares, however each is stored: the labels a generated migration's
/// `labels` step names on every dialect, also where the dialect keeps labels
/// as text in the rows and its plan holds no op for them (SQLite). The
/// planner's own label decider ([`missing_enum_labels`]) over the two
/// declarations, as [`plan_enum_label_removals`] reads them.
pub(crate) fn declared_label_additions(
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
) -> Vec<(String, String)> {
    let before = declared_enum_labels(&old.payload.models);
    let after = declared_enum_labels(&new.payload.models);
    let mut out = Vec::new();
    for (type_name, labels) in &after.labels {
        let Some(old_labels) = before.labels.get(type_name) else {
            continue;
        };
        for label in missing_enum_labels(labels, old_labels) {
            out.push((type_name.clone(), label));
        }
    }
    out
}

/// Type creation: every declared type the plan introduces
/// (`enum_type_provenance` over the columns it adds — a new table's, or an
/// existing table's new column) that `old` does not already hold.
fn plan_enum_type_creation(
    old: &Side,
    new: &Side,
    old_models: &BTreeMap<String, &SchemaModel>,
    plan: &mut Draft,
) {
    let declared = declared_enum_types(&new.ir().payload.models);
    let mut added_columns = Vec::new();
    let mut inline_created_columns = Vec::new();
    for model in &new.ir().payload.models {
        let old_model = old_models.get(&model.table_name);
        for col in &model.columns {
            let pair = (model.table_name.clone(), col.name.clone());
            match old_model {
                None => {
                    added_columns.push(pair.clone());
                    inline_created_columns.push(pair);
                }
                Some(old_model) if !old_model.columns.iter().any(|c| c.name == col.name) => {
                    added_columns.push(pair);
                }
                Some(_) => {}
            }
        }
    }
    for (type_name, provenance) in
        enum_type_provenance(&declared.declaring, &added_columns, &inline_created_columns)
    {
        if matches!(provenance, EnumTypeProvenance::Introduced { .. })
            && old.enum_labels(&type_name).is_none()
        {
            let labels = declared.labels.get(&type_name).cloned().unwrap_or_default();
            plan.operations
                .push(MigrationOp::CreateEnumType { type_name, labels });
        }
    }
}

/// Type drops (destructive; ADR-0020): every type the `old` snapshot declares
/// whose declaring columns the plan all removes, and that `new` no longer
/// declares anywhere.
fn plan_enum_type_drops(
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    old_models: &BTreeMap<String, &SchemaModel>,
    new_models: &BTreeMap<String, &SchemaModel>,
    plan: &mut Draft,
) {
    let before = declared_enum_types(&old.payload.models);
    let after = declared_enum_types(&new.payload.models);
    let mut removed_columns = Vec::new();
    let mut removed_with_table = Vec::new();
    for (table, old_model) in old_models {
        let new_model = new_models.get(table);
        for col in &old_model.columns {
            let pair = (table.clone(), col.name.clone());
            match new_model {
                None => {
                    removed_columns.push(pair.clone());
                    removed_with_table.push(pair);
                }
                Some(new_model) if !new_model.columns.iter().any(|c| c.name == col.name) => {
                    removed_columns.push(pair);
                }
                Some(_) => {}
            }
        }
    }
    for (type_name, provenance) in
        enum_type_provenance(&before.declaring, &removed_columns, &removed_with_table)
    {
        if matches!(provenance, EnumTypeProvenance::Introduced { .. })
            && !after.labels.contains_key(&type_name)
        {
            plan.operations
                .push(MigrationOp::DropEnumType { type_name });
        }
    }
}

/// Plan the [`MigrationOp::AddCheck`] operations for one table (#343;
/// ADR-0013): every check the target holds (`target_names`) — table check or
/// column check — that `live_check_names` does not already cover. The
/// decision is name-based and single-sourced in
/// `ferro_ddl_lowering::missing_check_names`.
fn missing_checks(
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    target_names: &[String],
    live_check_names: &[String],
) -> Vec<MigrationOp> {
    let riders = added_column_riders(old_model, new_model);
    missing_check_names(target_names, live_check_names)
        .into_iter()
        // A column check of an added column rides its `AddColumn`. Table
        // checks always stand alone.
        .filter(|name| !riders.iter().any(|riders| riders.has_check(name)))
        .map(|name| MigrationOp::AddCheck {
            table: table.to_string(),
            name,
        })
        .collect()
}

/// Plan the [`MigrationOp::RebuildCheck`] operations for one table (#344;
/// ADR-0015, ADR-0060): every check of `names` (each held ferro-owned on
/// both sides) whose body differs between them ([`check_differs`]).
fn check_rebuilds(
    old: &Side,
    new: &Side,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    names: &[&String],
    dialect: Dialect,
) -> Vec<MigrationOp> {
    names
        .iter()
        .filter(|name| check_differs(old, new, old_model, new_model, name, dialect))
        .map(|name| MigrationOp::RebuildCheck {
            table: new_model.table_name.clone(),
            name: (*name).clone(),
        })
        .collect()
}

/// Whether the check `name`'s body differs between `old_model` on `old` and
/// `new_model` on `new`. On SQLite, which stores the body as written, the one
/// normalizer compares the texts (ADR-0015, [`drifted_check_names`]). On
/// Postgres a live check compares with a declaration through what the live
/// read gathered, the printed pair: Postgres printed both bodies, so equal
/// strings are one predicate (ADR-0060). Two declarations, or two catalogs,
/// compare their texts. A body that does not render, or a live check with no
/// pair, differs: the rebuild then fails rendering it, naming the check.
fn check_differs(
    old: &Side,
    new: &Side,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    name: &str,
    dialect: Dialect,
) -> bool {
    let held = |side: &Side, model: &SchemaModel| {
        side.checks(model)
            .into_iter()
            .find(|(check, _)| check == name)
            .map(|(_, body)| body)
    };
    let text = |body: &Body, model: &SchemaModel| match body {
        Body::Canonical => declared_check_body(model, name, dialect).ok().flatten(),
        Body::Catalog(definition) => Some(definition.clone()),
    };
    let (Some(was), Some(now)) = (held(old, old_model), held(new, new_model)) else {
        return false;
    };
    let same_kind = matches!(
        (&was, &now),
        (Body::Canonical, Body::Canonical) | (Body::Catalog(_), Body::Catalog(_))
    );
    if dialect == Dialect::Sqlite || same_kind {
        let (Some(was), Some(now)) = (text(&was, old_model), text(&now, new_model)) else {
            return true;
        };
        return match dialect {
            Dialect::Sqlite => {
                !drifted_check_names(&[(name.to_string(), now)], &[(name.to_string(), was)])
                    .is_empty()
            }
            Dialect::Postgres => was != now,
        };
    }
    [old.table_facts(old_model), new.table_facts(new_model)]
        .iter()
        .find_map(|facts| {
            facts
                .checks
                .iter()
                .find(|check| check.name == name)?
                .printed
                .clone()
        })
        .is_none_or(|pair| pair.declared != pair.catalog)
}

/// Plan the [`MigrationOp::ValidateConstraint`] operations for one table
/// (#515; ADR-0043): every declared FK, then every declared CHECK (table
/// checks, then column checks), whose live constraint of the same name exists
/// `NOT VALID`.
fn validations(
    table: &str,
    new_model: &SchemaModel,
    live_fks: &[LiveFkValidity],
    live_checks: &[LiveCheckValidity],
) -> Vec<MigrationOp> {
    let unvalidated_fk = |name: &String| {
        live_fks
            .iter()
            .any(|live| &live.name == name && !live.validated)
    };
    let unvalidated_check = |name: &String| {
        live_checks
            .iter()
            .any(|live| &live.name == name && !live.validated)
    };
    let fk_names = new_model
        .foreign_keys
        .iter()
        .map(|fk| emit::fk_constraint_name(table, fk))
        .filter(unvalidated_fk);
    let check_names = declared_check_names(new_model)
        .into_iter()
        .filter(unvalidated_check);
    fk_names
        .chain(check_names)
        .map(|name| MigrationOp::ValidateConstraint {
            table: table.to_string(),
            name,
        })
        .collect()
}

/// Plan the [`MigrationOp::RebuildIndex`] operations for one table (#515;
/// ADR-0044): every declared standalone index or unique whose live index of
/// the same name exists but is invalid, in declared order.
fn index_rebuilds(
    table: &str,
    new_model: &SchemaModel,
    live_indexes: &[LiveIndexValidity],
) -> Vec<MigrationOp> {
    emit::standalone_indexes(new_model)
        .into_iter()
        .filter(|(name, _, _)| {
            live_indexes
                .iter()
                .any(|live| &live.name == name && !live.valid)
        })
        .map(|(name, columns, unique)| MigrationOp::RebuildIndex {
            table: table.to_string(),
            name,
            columns,
            unique,
        })
        .collect()
}

// -- the op verdict (ADR-0050) ----------------------------------------------------------

/// One op the planner decided, with its verdict: what is true of running it
/// between the plan's two sides on the plan's dialect.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct PlannedOp {
    /// The op, name-only.
    pub op: MigrationOp,
    /// Its verdict, computed once, when the plan is decided.
    pub verdict: OpVerdict,
}

/// What is true of one op, its two sides and the dialect — never which way
/// a file goes. Every door reads it and acts on it its own way: a demanding
/// column is a migration up's backfill, its down's `data-dependent` header,
/// and the bridge's plain op under a marker.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct OpVerdict {
    /// How the dialect runs it.
    pub execution: Execution,
    /// It asks existing rows for a value no statement supplies: a `NOT NULL`
    /// column added with no default, or a column made `NOT NULL`.
    pub demands_values: bool,
    /// It drops data: a table, a column, or the enum type that follows them.
    pub drops_data: bool,
    /// Whether its statements can fail on the rows the table holds.
    pub fails_on_rows: RowRisk,
    /// It brings back a table or a `NOT NULL` column, whose rows are gone.
    pub recreates: bool,
    /// It rides the statement of a column the same plan drops or adds.
    pub goes_with: Option<Rider>,
    /// It is the drop or the add of a stored generated column the plan
    /// drops and adds back, and why.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub replaces: Option<Replace>,
}

/// How the dialect runs an op.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Execution {
    /// The dialect's own statement, as the plan renders it.
    #[default]
    Native,
    /// A SQLite table rebuild (ADR-0046): only a generated migration writes
    /// it.
    Rebuild,
    /// No door runs it; the refusal says what to do instead.
    Refused(Refusal),
    /// A live target cannot express it (ADR-0050): the reason a revision's
    /// reader acts on.
    Irreversible(String),
}

/// Why no door runs an op, with the recipe that does it instead (AGENTS.md
/// I-6: every refusal names its fix).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The op changes a table's primary key (it moves to other columns,
    /// gains or loses one, or a key column's type changes).
    PrimaryKeyChange {
        /// The table.
        table: String,
    },
    /// The op moves a column to or from a native Postgres enum type, which
    /// no statement converts in place.
    EnumTypeMove {
        /// The table.
        table: String,
        /// The column.
        column: String,
        /// On a move to an enum type from a text column, the `db_type` token
        /// that keeps the values in a text column
        /// ([`ferro_ddl_lowering::string_storage_token`]).
        keep: Option<String>,
    },
    /// The op drops a generated column to add it back ([`Replace`]), and a
    /// row policy ferro cannot carry around the drop reads it: Postgres
    /// refuses to drop a column a policy reads.
    PolicyReadsReplacedColumn {
        /// The table.
        table: String,
        /// The generated column.
        column: String,
        /// The policy that reads it.
        policy: String,
    },
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Refusal::PrimaryKeyChange { table } => write!(
                f,
                "changing the primary key of \"{table}\" is not generated: write it as a new \
                 table (ferro migrate new --data-step …), a backfill of parent and children, \
                 and a drop; see the Migrations docs § Changing a primary key"
            ),
            Refusal::EnumTypeMove {
                table,
                column,
                keep,
            } => f.write_str(
                &ferro_ddl_lowering::enum_type_move_report(table, column, keep.as_deref()).text,
            ),
            Refusal::PolicyReadsReplacedColumn {
                table,
                column,
                policy,
            } => write!(
                f,
                "the generated column \"{table}\".\"{column}\" is dropped and added back \
                 for this change, and row policy \"{policy}\" reads it, which Postgres \
                 refuses to drop under a policy and ferro cannot carry around the drop: \
                 take the policy out of the model and migrate, make this change, then \
                 declare the policy again (or run `DROP POLICY \"{policy}\" ON \"{table}\"` \
                 before this change and create it again after)"
            ),
        }
    }
}

impl serde::Serialize for Refusal {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.to_string())
    }
}

/// Whether an op's statements can fail on the rows its table holds.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RowRisk {
    /// They cannot.
    #[default]
    None,
    /// They always scan the rows: a cast, a `SET NOT NULL`, a unique index,
    /// a `NOT NULL` column added with no value to give them.
    Always,
    /// A check or foreign key, which scans the rows when it is added
    /// validated; one added `NOT VALID` scans nothing until its validation.
    WhenValidated,
}

/// The column whose statement an op rides.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Rider {
    /// A check or index over a column the same plan drops, which the
    /// column's drop takes with it.
    DroppedColumn,
    /// An index, check or foreign key over a column the same plan adds.
    AddedColumn,
    /// A change an enum label's removal makes to a column that held it
    /// (#536): the column's own check rebuilt to the labels left, or its
    /// storage narrowed to them. It runs once no row holds the label.
    RemovedLabel,
}

impl PlannedOp {
    /// `op` with its verdict, between `before` and `target` on `dialect`.
    pub(crate) fn of(op: MigrationOp, before: &Side, target: &Side, dialect: Dialect) -> Self {
        let verdict = verdict(&op, before, target, dialect);
        PlannedOp { op, verdict }
    }
}

/// Whether existing rows need a value for `col` that no statement supplies:
/// it is `NOT NULL`, declares no default to fill them with, and is not
/// generated (the database computes a generated column for every row).
pub(crate) fn needs_values(col: &ferro_schema_ir::SchemaColumn) -> bool {
    !col.nullable
        && col.generated.is_none()
        && col.default.as_ref().is_none_or(serde_json::Value::is_null)
}

/// Whether `col` is stored as a native Postgres enum type.
fn native_enum(col: &ferro_schema_ir::SchemaColumn) -> bool {
    col.postgres_native_enum || enum_type_of(col).is_some()
}

/// The refusal of changing `table.column` from `old` to `new` when it moves
/// the column to, from or between native Postgres enum types, which no
/// statement converts in place ([`Refusal::EnumTypeMove`]); `None` when it
/// does not. A move to an enum type from a text column carries the token
/// that keeps the values in a text column. The verdict
/// refuses with it; the renderer reports it in the same words
/// ([`ferro_ddl_lowering::enum_type_move_report`]).
pub(crate) fn enum_type_move(
    table: &str,
    column: &str,
    old: &ferro_schema_ir::SchemaColumn,
    new: &ferro_schema_ir::SchemaColumn,
) -> Option<Refusal> {
    if !(native_enum(old) || native_enum(new)) {
        return None;
    }
    let keep = (!native_enum(old))
        .then(|| ferro_ddl_lowering::canonical_from_schema_column(old, Dialect::Postgres).ok())
        .flatten()
        .and_then(ferro_ddl_lowering::string_storage_token);
    Some(Refusal::EnumTypeMove {
        table: table.to_string(),
        column: column.to_string(),
        keep,
    })
}

fn find_column<'a>(
    model: Option<&'a SchemaModel>,
    name: &str,
) -> Option<&'a ferro_schema_ir::SchemaColumn> {
    model?.columns.iter().find(|col| col.name == name)
}

/// The verdict of `op` between `before` and `target` on `dialect`: the one
/// decision every door reads (ADR-0050).
fn verdict(op: &MigrationOp, before: &Side, target: &Side, dialect: Dialect) -> OpVerdict {
    let table = op.table();
    let was = table.and_then(|table| before.model(table));
    let now_values = table
        .and_then(|table| target.model(table))
        .map(|model| target.writable_values(model));
    let now = now_values.as_deref();
    let goes_with = goes_with(op, before, target).or_else(|| {
        (before.plans_label_removals() && rides_removed_label(op, was, now))
            .then_some(Rider::RemovedLabel)
    });
    // A generated column is computed for every row: its add asks the rows
    // for nothing and brings their values back with it, and can fail on any
    // of them (an overflow), as a change of its expression can.
    let generated_add = adds_generated(op, target);
    let replaces = op_replace(op, before, target, dialect);
    OpVerdict {
        execution: replace_refusal(op, before, target, dialect).map_or_else(
            || execution(op, was, now, goes_with, target, dialect),
            Execution::Refused,
        ),
        demands_values: !generated_add && demands_values(op, was, now),
        drops_data: match op {
            MigrationOp::DropTable { .. } | MigrationOp::DropEnumType { .. } => true,
            // A generated column dropped to be added back around a type
            // change loses nothing: the add computes every row again.
            MigrationOp::DropColumn { .. } => {
                !matches!(replaces, Some(Replace::InputType | Replace::OwnType))
            }
            _ => false,
        },
        fails_on_rows: if generated_add {
            RowRisk::Always
        } else {
            fails_on_rows(op, now)
        },
        recreates: match op {
            MigrationOp::AddTable { .. } => true,
            MigrationOp::AddColumn { column, .. } => {
                !generated_add && find_column(now, column).is_some_and(|col| !col.nullable)
            }
            _ => false,
        },
        goes_with,
        replaces,
    }
}

/// Why `op`, an add or a drop of a column, is half of a stored generated
/// column's drop and add back between `before` and `target` ([`replace_of`]).
fn op_replace(op: &MigrationOp, before: &Side, target: &Side, dialect: Dialect) -> Option<Replace> {
    let (MigrationOp::AddColumn { table, column } | MigrationOp::DropColumn { table, column }) = op
    else {
        return None;
    };
    let (was, now) = (before.model(table)?, target.model(table)?);
    replace_of(
        before,
        target,
        before.table_view(was, dialect).as_ref(),
        now,
        column,
        dialect,
    )
}

/// The refusal of `op` when it drops a generated column to add it back
/// ([`op_replace`]) and a row policy the plan cannot carry around the drop
/// reads it ([`policy_blocking_replace`]): Postgres refuses the drop. The
/// verdict refuses with it; the renderer fails with it in the same words.
pub(crate) fn replace_refusal(
    op: &MigrationOp,
    before: &Side,
    target: &Side,
    dialect: Dialect,
) -> Option<Refusal> {
    let MigrationOp::DropColumn { table, column } = op else {
        return None;
    };
    if dialect != Dialect::Postgres {
        return None;
    }
    op_replace(op, before, target, dialect)?;
    let policy = policy_blocking_replace(before, target, table, column)?;
    Some(Refusal::PolicyReadsReplacedColumn {
        table: table.clone(),
        column: column.clone(),
        policy,
    })
}

/// Whether `op` adds a column `target` holds as a stored generated one (a
/// live target by its catalog facts, which its IR does not carry).
fn adds_generated(op: &MigrationOp, target: &Side) -> bool {
    match op {
        MigrationOp::AddColumn { table, column } => target
            .model(table)
            .is_some_and(|model| target.holds_generated(model, column)),
        _ => false,
    }
}

/// Whether `op` asks the rows its table already holds (a table on both
/// sides) for a value no statement can supply: a new non-key column that
/// [`needs_values`], a nullable column made `NOT NULL`, or a removed enum
/// label rows may hold. A property of the models, never of the dialect.
fn demands_values(op: &MigrationOp, was: Option<&SchemaModel>, now: Option<&SchemaModel>) -> bool {
    match op {
        MigrationOp::AddColumn { column, .. } => {
            was.is_some()
                && find_column(now, column).is_some_and(|col| !col.primary_key && needs_values(col))
        }
        MigrationOp::AlterColumnNullability { column, .. } => matches!(
            (find_column(was, column), find_column(now, column)),
            (Some(old), Some(new))
                if old.nullable && !new.nullable && !old.primary_key && !new.primary_key
        ),
        MigrationOp::RemoveEnumLabel { .. } => true,
        _ => false,
    }
}

/// Whether `op`'s statements can fail on the rows its table holds, `now`
/// being its table on the target.
fn fails_on_rows(op: &MigrationOp, now: Option<&SchemaModel>) -> RowRisk {
    let always = |yes: bool| if yes { RowRisk::Always } else { RowRisk::None };
    match op {
        // Both recompute every row, which can overflow.
        MigrationOp::AlterColumnType { .. } | MigrationOp::AlterGeneratedExpression { .. } => {
            RowRisk::Always
        }
        MigrationOp::AlterColumnNullability { column, .. } => {
            always(find_column(now, column).is_some_and(|col| !col.nullable))
        }
        MigrationOp::AddIndex { unique, .. } => always(*unique),
        MigrationOp::AddCheck { .. }
        | MigrationOp::RebuildCheck { .. }
        | MigrationOp::AddForeignKey { .. }
        | MigrationOp::RebuildForeignKey { .. } => RowRisk::WhenValidated,
        MigrationOp::AddColumn { column, .. } => always(
            find_column(now, column)
                .is_some_and(|col| needs_values(col) || (!col.nullable && col.unique)),
        ),
        _ => RowRisk::None,
    }
}

/// The column whose statement `op`, planned from `before` to `target`, rides:
/// a dropped column's own check ([`Side::column_check`], which Postgres and
/// SQLite both drop with the column: SQLite's `DROP COLUMN` takes the
/// `CHECK` written inline on the column, and refuses only for one a
/// table-level `CHECK` reads) or an index over a dropped column; or an
/// index, a column check or a foreign key over a column the plan adds — a
/// rider of the added column ([`emit::column_riders`]), or a composite index
/// over it. The renderer reads it too: a check riding its column's drop has
/// no statement of its own on SQLite.
pub(crate) fn goes_with(op: &MigrationOp, before: &Side, target: &Side) -> Option<Rider> {
    let table = op.table()?;
    let (Some(was), Some(now)) = (before.model(table), target.model(table)) else {
        return None;
    };
    let dropped = |name: &str| {
        find_column(Some(was), name).is_some() && find_column(Some(now), name).is_none()
    };
    let added = |name: &str| {
        find_column(Some(was), name).is_none() && find_column(Some(now), name).is_some()
    };
    let riders = || {
        now.columns
            .iter()
            .filter(|col| added(&col.name))
            .map(|col| emit::column_riders(now, &col.name))
    };
    match op {
        MigrationOp::DropCheck { name, .. } => was
            .columns
            .iter()
            .filter(|col| dropped(&col.name))
            .any(|col| before.column_check(was, &col.name).as_ref() == Some(name))
            .then_some(Rider::DroppedColumn),
        MigrationOp::DropIndex { name, .. } => emit::standalone_indexes(was)
            .into_iter()
            .any(|(index, columns, _)| &index == name && columns.iter().any(|c| dropped(c)))
            .then_some(Rider::DroppedColumn),
        MigrationOp::AddIndex { columns, .. } => columns
            .iter()
            .any(|name| added(name))
            .then_some(Rider::AddedColumn),
        MigrationOp::AddCheck { name, .. } => riders()
            .any(|riders| riders.has_check(name))
            .then_some(Rider::AddedColumn),
        MigrationOp::AddForeignKey { column, .. } => riders()
            .any(|riders| riders.foreign_key.is_some_and(|fk| &fk.column == column))
            .then_some(Rider::AddedColumn),
        _ => None,
    }
}

/// Whether `op` is a change a removed enum label makes to the column that
/// held it: its own check rebuilt, or its storage changed, while the
/// column's enum keeps its type and drops a label `was` declares.
fn rides_removed_label(
    op: &MigrationOp,
    was: Option<&SchemaModel>,
    now: Option<&SchemaModel>,
) -> bool {
    let column = match op {
        MigrationOp::AlterColumnType { column, .. } => column.as_str(),
        MigrationOp::RebuildCheck { name, .. } => {
            match now.and_then(|model| model.checks.iter().find(|check| &check.name == name)) {
                Some(check) => check.column.as_str(),
                None => return false,
            }
        }
        _ => return false,
    };
    let Some(declared) = find_column(now, column) else {
        return false;
    };
    // A label a declared hint renames away is a rename, never a removal.
    let renamed_away: Vec<&String> = declared
        .enum_renamed_labels
        .as_ref()
        .map(|hints| hints.labels.values().collect())
        .unwrap_or_default();
    match (
        find_column(was, column).and_then(enum_declaration),
        enum_declaration(declared),
    ) {
        (Some((old_type, old)), Some((new_type, new))) => {
            old_type == new_type
                && old
                    .iter()
                    .any(|label| !new.contains(label) && !renamed_away.contains(&label))
        }
        _ => false,
    }
}

/// How `dialect` runs `op` toward `target`: what the target cannot express
/// is irreversible; a primary-key change and a move to or from a native
/// enum type are refused with their recipe; what SQLite's `ALTER TABLE`
/// cannot do ([`sqlite_rebuilds`]) is a rebuild.
fn execution(
    op: &MigrationOp,
    was: Option<&SchemaModel>,
    now: Option<&SchemaModel>,
    goes_with: Option<Rider>,
    target: &Side,
    dialect: Dialect,
) -> Execution {
    if let Err(reason) = target.expresses(op) {
        return Execution::Irreversible(reason);
    }
    let table = || op.table().unwrap_or_default().to_string();
    let key = |col: Option<&ferro_schema_ir::SchemaColumn>| col.is_some_and(|col| col.primary_key);
    let primary_key = match op {
        MigrationOp::ChangePrimaryKey { .. } => true,
        MigrationOp::AddColumn { column, .. } => key(find_column(now, column)),
        MigrationOp::DropColumn { column, .. } => key(find_column(was, column)),
        MigrationOp::AlterColumnType { column, .. }
        | MigrationOp::AlterColumnNullability { column, .. } => {
            key(find_column(was, column)) || key(find_column(now, column))
        }
        _ => false,
    };
    if primary_key {
        return Execution::Refused(Refusal::PrimaryKeyChange { table: table() });
    }
    // SQLite's `ADD COLUMN` takes no stored generated column.
    if dialect == Dialect::Sqlite
        && (sqlite_rebuilds(op, was, now, goes_with) || adds_generated(op, target))
    {
        return Execution::Rebuild;
    }
    if let MigrationOp::AlterColumnType { column, .. } = op
        && let (Some(old), Some(new)) = (find_column(was, column), find_column(now, column))
        && let Some(refusal) = enum_type_move(&table(), column, old, new)
    {
        // To, from or between native enum types: no statement converts the
        // column in place.
        return Execution::Refused(refusal);
    }
    Execution::Native
}

/// Whether SQLite can run `op` only through a table rebuild (ADR-0046).
///
/// | Op | SQLite |
/// | :-- | :-- |
/// | add/drop a table, an enum type or label, an index | native |
/// | redefine an index (drop + create) | native |
/// | rename a table, a column, an index (drop + create), a policy | native |
/// | rename a constraint (a `ck_` / `fk_` name a rename drags) | rebuild |
/// | add an optional column | native |
/// | add a required column (SQLite's `ADD COLUMN … NOT NULL` keeps its `DEFAULT` for good, and no `SET NOT NULL` reaches it) | rebuild |
/// | drop a plain column | native |
/// | drop a foreign-key column | rebuild |
/// | change a column's type or nullability | rebuild |
/// | add a generated column (decided by the caller, from the target's side), make one plain, or change its expression | rebuild |
/// | drop a column to add it back generated | rebuild |
/// | drop a foreign default | rebuild |
/// | add, change or drop a check (but a dropped column's own) | rebuild |
/// | add, retarget or drop a foreign key (on a kept column) | rebuild |
/// | change the primary key | rebuild |
/// | validate a constraint, rebuild an invalid index, row security | native (nothing on SQLite) |
///
/// A foreign-key column comes off natively only while it is the inline
/// `REFERENCES` column `ADD COLUMN` wrote; once any rebuild has written the
/// table (as `FOREIGN KEY (…)`, `CREATE TABLE`'s shape) SQLite refuses to
/// drop it in place, and nothing can know which shape it finds, so the drop
/// is always a rebuild.
fn sqlite_rebuilds(
    op: &MigrationOp,
    was: Option<&SchemaModel>,
    now: Option<&SchemaModel>,
    goes_with: Option<Rider>,
) -> bool {
    let has_foreign_key = |model: Option<&SchemaModel>, column: &str| {
        model.is_some_and(|model| model.foreign_keys.iter().any(|fk| fk.column == column))
    };
    match op {
        MigrationOp::AddTable { .. }
        | MigrationOp::DropTable { .. }
        | MigrationOp::CreateEnumType { .. }
        | MigrationOp::DropEnumType { .. }
        | MigrationOp::AddEnumLabel { .. }
        | MigrationOp::RenameEnumLabel { .. }
        | MigrationOp::RemoveEnumLabel { .. }
        | MigrationOp::RenameEnumType { .. }
        | MigrationOp::AddIndex { .. }
        | MigrationOp::DropIndex { .. }
        | MigrationOp::RedefineIndex { .. }
        | MigrationOp::RebuildIndex { .. }
        | MigrationOp::ValidateConstraint { .. }
        | MigrationOp::AddRowPolicy { .. }
        | MigrationOp::RebuildRowPolicy { .. }
        | MigrationOp::DropRowPolicy { .. }
        | MigrationOp::EnableRowSecurity { .. }
        | MigrationOp::ForceRowSecurity { .. }
        | MigrationOp::DisableRowSecurity { .. }
        | MigrationOp::NoForceRowSecurity { .. }
        | MigrationOp::RenameTable { .. }
        | MigrationOp::RenameColumn { .. }
        | MigrationOp::RenameIndex { .. }
        | MigrationOp::RenamePolicy { .. } => false,
        // A table constraint's name lives in `CREATE TABLE` (ADR-0046).
        MigrationOp::RenameConstraint { .. } => true,
        // ferro persists no server default (ADR-0027), and SQLite has no
        // `ALTER COLUMN … DROP DEFAULT` to take a backfill `DEFAULT` off
        // again: only a rebuild adds a column `NOT NULL` and default-free.
        // The pass, which never rebuilds, adds one with a literal default as
        // `NOT NULL DEFAULT <literal>` (ADR-0034, the SQLite exception).
        MigrationOp::AddColumn { column, .. } => {
            find_column(now, column).is_some_and(|col| !col.nullable)
        }
        // A column the target still holds is dropped to be added back
        // generated, which only the rebuild does.
        MigrationOp::DropColumn { column, .. } => {
            has_foreign_key(was, column) || find_column(now, column).is_some()
        }
        // SQLite has no `ALTER COLUMN … DROP DEFAULT`.
        MigrationOp::AlterColumnType { .. }
        | MigrationOp::AlterGeneratedExpression { .. }
        | MigrationOp::DropGeneratedExpression { .. }
        | MigrationOp::AlterColumnNullability { .. }
        | MigrationOp::DropDefault { .. }
        | MigrationOp::ChangePrimaryKey { .. }
        | MigrationOp::AddCheck { .. }
        | MigrationOp::RebuildCheck { .. }
        | MigrationOp::AddForeignKey { .. }
        | MigrationOp::DropForeignKey { .. }
        | MigrationOp::RebuildForeignKey { .. } => true,
        MigrationOp::DropCheck { .. } => goes_with != Some(Rider::DroppedColumn),
    }
}

// -- a down (ADR-0050) --------------------------------------------------------------------

/// The ops that undo the step `up` (planned from `before` to `after`): the
/// planner run from `after` back to `before` on `dialect`, keeping only the
/// ops whose artifact the up touched (ADR-0050). Both doors use it: a
/// generated step's down, between the stages on either side of the step,
/// and the Alembic bridge's `downgrade()`, from the models to the database.
///
/// - **Renames** come from the up's own rename ops, swapped: the hints they
///   give back are planned like any declared hint, so a down
///   needs no hints of its own and its renames run first.
/// - **Destructive changes are on**: a column, table or index the up added
///   is dropped by the down. That is a planning option, never a header.
/// - **The scope is the artifact**, not the table or the type: a column, an
///   index, a named constraint (a foreign key by its column), a policy, a
///   row-security flag, an enum type, one enum label, a table — a table's
///   scope holding everything on it. An added column's scope includes the
///   index, check and foreign key that ride its statement. Scoping keeps the
///   warn-only categories one-way: an extra live label, a foreign policy or a
///   leftover check the up left alone is never "restored".
///
/// What a live `before` cannot express is the op's
/// [`Execution::Irreversible`] verdict; between two declared stages no op
/// is (ADR-0033).
pub fn plan_down(up: &[MigrationOp], after: &Side, before: &Side, dialect: Dialect) -> Plan {
    let hints = hints_undoing(up);
    let mut plan = plan_with_hints(
        after,
        before,
        &hints,
        dialect,
        PlanOptions { destructive: true },
    );
    let mut back = KeyRenames::of(plan.ops().filter(|op| is_rename(op)));
    back.undo_names(up);
    let mut scope: BTreeSet<Artifact> = BTreeSet::new();
    for op in up {
        scope.extend(key(op).map(|key| back.apply(key)));
        // What rides an added or a dropped column's statement is its scope
        // too ([`emit::column_riders`]): read where the column stands.
        let riders = match op {
            MigrationOp::AddColumn { table, column } => after
                .model(table)
                .map(|model| emit::column_riders(model, column))
                .map(|riders| (back.table(table), riders, true)),
            MigrationOp::DropColumn { table, column } => {
                let table = back.table(table);
                let column = back.column(&table, column);
                before
                    .model(&table)
                    .map(|model| emit::column_riders(model, &column))
                    .map(|riders| (table, riders, false))
            }
            _ => None,
        };
        // An added column's riders are named as the up names them; a dropped
        // one's are read from `before`, already under the down's names.
        if let Some((table, riders, up_names)) = riders {
            let renamed = |name: &str| {
                if up_names {
                    back.name(&table, name)
                } else {
                    name.to_string()
                }
            };
            scope.extend(
                riders
                    .index
                    .iter()
                    .chain(&riders.unique)
                    .map(|name| Artifact::Index(table.clone(), renamed(name))),
            );
            scope.extend(
                riders
                    .check
                    .map(|check| Artifact::Constraint(table.clone(), renamed(&check.name))),
            );
            scope.extend(riders.foreign_key.map(|fk| {
                let column = if up_names {
                    back.column(&table, &fk.column)
                } else {
                    fk.column.clone()
                };
                Artifact::ForeignKey(table.clone(), column)
            }));
        }
    }
    // A column in scope that the down drops and adds back (made generated
    // again) takes every index, check and foreign key over it with the
    // drop: the down puts them back too, as `before` holds them.
    let added_back: Vec<(String, String)> = plan
        .ops()
        .filter_map(|op| match op {
            MigrationOp::DropColumn { table, column }
                if scope.contains(&Artifact::Column(table.clone(), column.clone()))
                    && plan.ops().any(|other| {
                        matches!(other, MigrationOp::AddColumn { table: t, column: c }
                            if t == table && c == column)
                    }) =>
            {
                Some((table.clone(), column.clone()))
            }
            _ => None,
        })
        .collect();
    for (table, column) in added_back {
        let Some(model) = before.model(&table) else {
            continue;
        };
        scope.extend(
            emit::standalone_indexes(model)
                .into_iter()
                .filter(|(_, columns, _)| columns.contains(&column))
                .map(|(name, ..)| Artifact::Index(table.clone(), name)),
        );
        let riders = emit::column_riders(model, &column);
        scope.extend(
            riders
                .check
                .map(|check| Artifact::Constraint(table.clone(), check.name.clone()))
                .into_iter()
                .chain(
                    checks_reading(model, std::slice::from_ref(&column))
                        .into_iter()
                        .map(|name| Artifact::Constraint(table.clone(), name)),
                ),
        );
        scope.extend(
            riders
                .foreign_key
                .map(|_| Artifact::ForeignKey(table.clone(), column.clone())),
        );
    }
    plan.operations.retain(|planned| {
        is_rename(&planned.op)
            || planned
                .op
                .table()
                .is_some_and(|table| scope.contains(&Artifact::Table(table.to_string())))
            || key(&planned.op).is_some_and(|key| scope.contains(&key))
    });
    plan
}

/// The hints that undo the renames `up` runs: each table, column, enum type
/// and label rename the other way round, by the names the down plans with.
fn hints_undoing(up: &[MigrationOp]) -> Vec<Hint> {
    let old_table = |table: &str| {
        up.iter()
            .find_map(|op| match op {
                MigrationOp::RenameTable { old, new } if new == table => Some(old.clone()),
                _ => None,
            })
            .unwrap_or_else(|| table.to_string())
    };
    let old_type = |type_name: &str| {
        up.iter()
            .find_map(|op| match op {
                MigrationOp::RenameEnumType { old, new } if new == type_name => Some(old.clone()),
                _ => None,
            })
            .unwrap_or_else(|| type_name.to_string())
    };
    up.iter()
        .filter_map(|op| match op {
            MigrationOp::RenameTable { old, new } => Some(Hint::Table {
                old: new.clone(),
                new: old.clone(),
            }),
            MigrationOp::RenameColumn { table, old, new } => Some(Hint::Column {
                table: old_table(table),
                old: new.clone(),
                new: old.clone(),
            }),
            MigrationOp::RenameEnumType { old, new } => Some(Hint::EnumType {
                old: new.clone(),
                new: old.clone(),
            }),
            MigrationOp::RenameEnumLabel {
                type_name,
                old,
                new,
                ..
            } => Some(Hint::Label {
                type_name: old_type(type_name),
                old: new.clone(),
                new: old.clone(),
            }),
            _ => None,
        })
        .collect()
}

/// One artifact an op touches: what scopes a down (ADR-0050).
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Artifact {
    /// A whole table, everything on it included.
    Table(String),
    /// A column.
    Column(String, String),
    /// A column's server default.
    Default(String, String),
    /// A table's primary key.
    PrimaryKey(String),
    /// A standalone index, by name.
    Index(String, String),
    /// A named check.
    Constraint(String, String),
    /// A foreign key, by its column: one per column, and its name follows
    /// its target.
    ForeignKey(String, String),
    /// A row policy, by name.
    Policy(String, String),
    /// A table's `ENABLE` flag (`true`) or its `FORCE` flag (`false`).
    Flag(String, bool),
    /// A native enum type.
    EnumType(String),
    /// One label of an enum type.
    Label(String, String),
}

/// The artifact `op` touches, or `None` for a rename (a down takes the up's
/// renames whole).
fn key(op: &MigrationOp) -> Option<Artifact> {
    let t = |table: &String| table.clone();
    Some(match op {
        MigrationOp::AddEnumLabel { type_name, label }
        | MigrationOp::RemoveEnumLabel {
            type_name, label, ..
        } => Artifact::Label(type_name.clone(), label.clone()),
        MigrationOp::CreateEnumType { type_name, .. } | MigrationOp::DropEnumType { type_name } => {
            Artifact::EnumType(type_name.clone())
        }
        MigrationOp::AddTable { table } | MigrationOp::DropTable { table } => {
            Artifact::Table(t(table))
        }
        MigrationOp::AddColumn { table, column }
        | MigrationOp::DropColumn { table, column }
        | MigrationOp::AlterColumnType { table, column }
        | MigrationOp::AlterGeneratedExpression { table, column }
        | MigrationOp::DropGeneratedExpression { table, column }
        | MigrationOp::AlterColumnNullability { table, column } => {
            Artifact::Column(t(table), column.clone())
        }
        MigrationOp::DropDefault { table, column, .. } => {
            Artifact::Default(t(table), column.clone())
        }
        MigrationOp::ChangePrimaryKey { table, .. } => Artifact::PrimaryKey(t(table)),
        MigrationOp::AddIndex { table, name, .. }
        | MigrationOp::DropIndex { table, name }
        | MigrationOp::RedefineIndex { table, name }
        | MigrationOp::RebuildIndex { table, name, .. } => Artifact::Index(t(table), name.clone()),
        MigrationOp::AddForeignKey { table, column }
        | MigrationOp::DropForeignKey { table, column, .. }
        | MigrationOp::RebuildForeignKey { table, column, .. } => {
            Artifact::ForeignKey(t(table), column.clone())
        }
        MigrationOp::AddCheck { table, name }
        | MigrationOp::RebuildCheck { table, name }
        | MigrationOp::DropCheck { table, name }
        | MigrationOp::ValidateConstraint { table, name } => {
            Artifact::Constraint(t(table), name.clone())
        }
        MigrationOp::AddRowPolicy { table, name }
        | MigrationOp::RebuildRowPolicy { table, name }
        | MigrationOp::DropRowPolicy { table, name } => Artifact::Policy(t(table), name.clone()),
        MigrationOp::EnableRowSecurity { table } | MigrationOp::DisableRowSecurity { table } => {
            Artifact::Flag(t(table), true)
        }
        MigrationOp::ForceRowSecurity { table } | MigrationOp::NoForceRowSecurity { table } => {
            Artifact::Flag(t(table), false)
        }
        MigrationOp::RenameTable { .. }
        | MigrationOp::RenameColumn { .. }
        | MigrationOp::RenameIndex { .. }
        | MigrationOp::RenameConstraint { .. }
        | MigrationOp::RenamePolicy { .. }
        | MigrationOp::RenameEnumLabel { .. }
        | MigrationOp::RenameEnumType { .. } => return None,
    })
}

/// A down's renames as a map from the up's names to the down's: what turns
/// the key of an up op into the key of the down op that undoes it.
#[derive(Default)]
struct KeyRenames {
    tables: BTreeMap<String, String>,
    columns: BTreeMap<(String, String), String>,
    names: BTreeMap<(String, String), String>,
    types: BTreeMap<String, String>,
    labels: BTreeMap<(String, String), String>,
}

impl KeyRenames {
    fn of<'a>(renames: impl Iterator<Item = &'a MigrationOp>) -> Self {
        let mut map = KeyRenames::default();
        for op in renames {
            match op {
                MigrationOp::RenameTable { old, new } => {
                    map.tables.insert(old.clone(), new.clone());
                }
                MigrationOp::RenameColumn { table, old, new } => {
                    map.columns
                        .insert((table.clone(), old.clone()), new.clone());
                }
                MigrationOp::RenameIndex { table, old, new }
                | MigrationOp::RenameConstraint { table, old, new }
                | MigrationOp::RenamePolicy { table, old, new } => {
                    map.names.insert((table.clone(), old.clone()), new.clone());
                }
                MigrationOp::RenameEnumType { old, new } => {
                    map.types.insert(old.clone(), new.clone());
                }
                MigrationOp::RenameEnumLabel {
                    type_name,
                    old,
                    new,
                    ..
                } => {
                    map.labels
                        .insert((type_name.clone(), old.clone()), new.clone());
                }
                _ => {}
            }
        }
        map
    }

    /// Each index, constraint and policy name `up` renames that the down's
    /// own renames do not rename back — a name only a live database's facts
    /// carry (a leftover check on a renamed table, `fact_renames`) — mapped
    /// back to the name it had: the down's keys are compared after the up's
    /// renames are undone, so the down restores the artifact under its old
    /// name.
    fn undo_names(&mut self, up: &[MigrationOp]) {
        for op in up {
            if let MigrationOp::RenameIndex { table, old, new }
            | MigrationOp::RenameConstraint { table, old, new }
            | MigrationOp::RenamePolicy { table, old, new } = op
            {
                let key = (self.table(table), new.clone());
                self.names.entry(key).or_insert_with(|| old.clone());
            }
        }
    }

    fn table(&self, table: &str) -> String {
        self.tables
            .get(table)
            .cloned()
            .unwrap_or_else(|| table.to_string())
    }

    /// Column `column` of `table` (already the down's table name).
    fn column(&self, table: &str, column: &str) -> String {
        self.columns
            .get(&(table.to_string(), column.to_string()))
            .cloned()
            .unwrap_or_else(|| column.to_string())
    }

    /// Index, constraint or policy `name` on `table` (the down's name).
    fn name(&self, table: &str, name: &str) -> String {
        self.names
            .get(&(table.to_string(), name.to_string()))
            .cloned()
            .unwrap_or_else(|| name.to_string())
    }

    fn apply(&self, key: Artifact) -> Artifact {
        match key {
            Artifact::Table(t) => Artifact::Table(self.table(&t)),
            Artifact::Column(t, c) => {
                let t = self.table(&t);
                let c = self.column(&t, &c);
                Artifact::Column(t, c)
            }
            Artifact::Default(t, c) => {
                let t = self.table(&t);
                let c = self.column(&t, &c);
                Artifact::Default(t, c)
            }
            Artifact::PrimaryKey(t) => Artifact::PrimaryKey(self.table(&t)),
            Artifact::Index(t, n) => {
                let t = self.table(&t);
                let n = self.name(&t, &n);
                Artifact::Index(t, n)
            }
            Artifact::Constraint(t, n) => {
                let t = self.table(&t);
                let n = self.name(&t, &n);
                Artifact::Constraint(t, n)
            }
            Artifact::ForeignKey(t, c) => {
                let t = self.table(&t);
                let c = self.column(&t, &c);
                Artifact::ForeignKey(t, c)
            }
            Artifact::Policy(t, n) => {
                let t = self.table(&t);
                let n = self.name(&t, &n);
                Artifact::Policy(t, n)
            }
            Artifact::Flag(t, flag) => Artifact::Flag(self.table(&t), flag),
            Artifact::EnumType(name) => {
                Artifact::EnumType(self.types.get(&name).cloned().unwrap_or(name))
            }
            Artifact::Label(type_name, label) => {
                let type_name = self.types.get(&type_name).cloned().unwrap_or(type_name);
                let label = self
                    .labels
                    .get(&(type_name.clone(), label.clone()))
                    .cloned()
                    .unwrap_or(label);
                Artifact::Label(type_name, label)
            }
        }
    }
}

// -- rename hints (ADR-0032) ----------------------------------------------------------

/// One live rename hint (ADR-0032): a declaration on the new snapshot saying
/// what a table or a column was called in the old one, and live because the
/// old snapshot still holds that name and lacks the new one.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(tag = "kind")]
pub enum Hint {
    /// `__ferro_renamed_from__`: table `old` is now `new`.
    Table {
        /// The table's name in the old snapshot.
        old: String,
        /// Its name in the new one.
        new: String,
    },
    /// `renamed_from=`: column `old` is now `new`.
    Column {
        /// The owning table as the new snapshot names it.
        table: String,
        /// The column's name in the old snapshot.
        old: String,
        /// Its name in the new one.
        new: String,
    },
    /// No declaration: every column of the old snapshot's enum type `old`
    /// now declares the type `new`, which the old snapshot lacks, and `old`
    /// is declared nowhere any more. An enum type's rename takes no hint; it
    /// is read off the columns that moved to it (ADR-0032).
    EnumType {
        /// The type's name in the old snapshot.
        old: String,
        /// Its name in the new one.
        new: String,
    },
    /// `__ferro_renamed_labels__ = {new: old}` on the enum class: label `old`
    /// of the type is now `new`.
    Label {
        /// The type as the new snapshot names it.
        type_name: String,
        /// The label in the old snapshot.
        old: String,
        /// The label in the new one.
        new: String,
    },
}

/// Refuse the rename hints `new` declares that cannot be meant (ADR-0032),
/// live or inert: an old name `new` still declares, and two declarations on
/// one old name. Checked over the declarations alone, so every door that
/// reads a hint refuses the same hints with the same text.
///
/// # Errors
/// The first [`HintError`] found: each model's table hint, then its column
/// hints, in model order; then two tables on one old name; then labels.
pub fn refuse_hints(new: &SchemaIrPayload) -> Result<(), HintError> {
    let mut table_claims: Vec<(String, Vec<String>)> = Vec::new();
    for model in &new.models {
        if let Some(previous) = &model.renamed_from {
            if new.models.iter().any(|m| &m.table_name == previous) {
                return Err(HintError::OldStillDeclared {
                    table: model.table_name.clone(),
                    field: None,
                    old: previous.clone(),
                });
            }
            claim(&mut table_claims, previous, &model.table_name);
        }
        let mut column_claims: Vec<(String, Vec<String>)> = Vec::new();
        for col in &model.columns {
            let Some(previous) = &col.renamed_from else {
                continue;
            };
            if model.columns.iter().any(|c| &c.name == previous) {
                return Err(HintError::OldStillDeclared {
                    table: model.table_name.clone(),
                    field: Some(col.name.clone()),
                    old: previous.clone(),
                });
            }
            claim(&mut column_claims, previous, &col.name);
        }
        if let Some((previous, claimants)) = column_claims.into_iter().find(|(_, c)| c.len() > 1) {
            return Err(HintError::Ambiguous {
                table: Some(model.table_name.clone()),
                old: previous,
                claimants,
            });
        }
    }
    if let Some((previous, claimants)) = table_claims.into_iter().find(|(_, c)| c.len() > 1) {
        return Err(HintError::Ambiguous {
            table: None,
            old: previous,
            claimants,
        });
    }
    // Label hints are checked over every enum, how ever it is stored.
    let new_labels = declared_enum_labels(&new.models);
    for (type_name, renamed) in &new_labels.renamed_labels {
        let labels = new_labels
            .labels
            .get(type_name)
            .cloned()
            .unwrap_or_default();
        let mut label_claims: Vec<(String, Vec<String>)> = Vec::new();
        for (label, previous) in &renamed.labels {
            if labels.contains(previous) {
                return Err(HintError::LabelStillDeclared {
                    enum_class: renamed.enum_class.clone(),
                    type_name: type_name.clone(),
                    new: label.clone(),
                    old: previous.clone(),
                });
            }
            claim(&mut label_claims, previous, label);
        }
        if let Some((previous, claimants)) = label_claims.into_iter().find(|(_, c)| c.len() > 1) {
            return Err(HintError::AmbiguousLabel {
                enum_class: renamed.enum_class.clone(),
                type_name: type_name.clone(),
                old: previous,
                claimants,
            });
        }
    }
    Ok(())
}

/// The old name `model`'s table hint renames from, when the hint is live
/// against a schema whose tables `holds` answers for (ADR-0032): it holds
/// the old table and lacks the new one. `None` for an inert hint or none.
pub fn table_rename_hint(model: &SchemaModel, holds: impl Fn(&str) -> bool) -> Option<&str> {
    model
        .renamed_from
        .as_deref()
        .filter(|previous| holds(previous) && !holds(&model.table_name))
}

/// The live table rename hints `new` declares against a live database whose
/// table names are `live_tables`, in model order: what [`live_hints`] decides
/// for tables when the live side is read with every hinted old table. The
/// read side asks this before it reads, to know which undeclared live tables
/// a plan renames and so must cover.
///
/// # Errors
/// [`refuse_hints`]'s refusal, under which no hint renames anything.
pub fn live_table_hints(
    live_tables: &BTreeSet<String>,
    new: &SchemaIrPayload,
) -> Result<Vec<Hint>, HintError> {
    refuse_hints(new)?;
    Ok(new
        .models
        .iter()
        .filter_map(|model| {
            table_rename_hint(model, |table| live_tables.contains(table)).map(|old| Hint::Table {
                old: old.to_string(),
                new: model.table_name.clone(),
            })
        })
        .collect())
}

/// The live rename hints `new` declares against `old` (ADR-0032), tables
/// first, then columns, in model order.
///
/// A table hint is live when `old` holds the old table and lacks the new one;
/// a column hint when the old table (the hinted table's old name, under a
/// live table hint) holds the old column and lacks the new one. Every other
/// hint is inert, which is every hint after its migration.
///
/// # Errors
/// [`HintError::OldStillDeclared`] for a hint whose old name `new` still
/// declares, and [`HintError::Ambiguous`] for two hints on one old name —
/// checked for every hint, live or not.
pub fn live_hints(old: &SchemaIrPayload, new: &SchemaIrPayload) -> Result<Vec<Hint>, HintError> {
    refuse_hints(new)?;
    // Label renames are matched over every enum, how ever it is stored; type
    // renames only over the native types, the ones that exist as objects.
    let new_labels = declared_enum_labels(&new.models);
    let new_types = declared_enum_types(&new.models);

    let old_tables = index_models(&old.models);
    let mut tables = Vec::new();
    let mut columns = Vec::new();
    for model in &new.models {
        let table_hint = table_rename_hint(model, |table| old_tables.contains_key(table));
        if let Some(previous) = table_hint {
            tables.push(Hint::Table {
                old: previous.to_string(),
                new: model.table_name.clone(),
            });
        }
        let Some(old_model) = old_tables.get(table_hint.unwrap_or(&model.table_name)) else {
            continue;
        };
        let holds = |name: &str| old_model.columns.iter().any(|c| c.name == name);
        for col in &model.columns {
            if let Some(previous) = &col.renamed_from
                && holds(previous)
                && !holds(&col.name)
            {
                columns.push(Hint::Column {
                    table: model.table_name.clone(),
                    old: previous.clone(),
                    new: col.name.clone(),
                });
            }
        }
    }
    tables.extend(columns);

    // Enum types and labels are matched against `old` as the table and
    // column renames leave it: a column is the same column under its new name.
    let renamed_old: Vec<SchemaModel> = {
        let renames = Renames { hints: &tables };
        old.models
            .iter()
            .map(|model| renamed_model(model, &renames))
            .collect()
    };
    let old_types = declared_enum_types(&renamed_old);
    let old_labels = declared_enum_labels(&renamed_old);
    let types = enum_type_renames(&old_types, &new_types, &new.models);
    let mut labels = Vec::new();
    for (type_name, renamed) in &new_labels.renamed_labels {
        let old_name = types
            .iter()
            .find_map(|hint| match hint {
                Hint::EnumType { old, new } if new == type_name => Some(old.as_str()),
                _ => None,
            })
            .unwrap_or(type_name);
        let Some(held) = old_labels.labels.get(old_name) else {
            continue;
        };
        for (label, previous) in &renamed.labels {
            if held.contains(previous) && !held.contains(label) {
                labels.push(Hint::Label {
                    type_name: type_name.clone(),
                    old: previous.clone(),
                    new: label.clone(),
                });
            }
        }
    }
    tables.extend(types);
    tables.extend(labels);
    Ok(tables)
}

/// The enum type renames the columns imply (ADR-0032): a type `A` the old
/// snapshot declares and the new one does not, every column of which now
/// declares one and the same type `B` that the old snapshot lacks and no
/// other vanished type moved to. Anything less — a column of `A` dropped,
/// split across two types, or moved to a type that already exists — is no
/// rename: the plain column type change is planned instead.
fn enum_type_renames(
    old_types: &DeclaredEnumTypes,
    new_types: &DeclaredEnumTypes,
    new_models: &[SchemaModel],
) -> Vec<Hint> {
    let new_type_of = |table: &str, column: &str| {
        new_models
            .iter()
            .find(|model| model.table_name == table)?
            .columns
            .iter()
            .find(|col| col.name == column)
            .and_then(enum_type_of)
            .map(|(type_name, _)| type_name)
    };
    let mut candidates: Vec<(String, String)> = Vec::new();
    for (old, columns) in &old_types.declaring {
        if new_types.labels.contains_key(old) {
            continue;
        }
        let moved: BTreeSet<Option<String>> = columns
            .iter()
            .map(|(table, column)| new_type_of(table, column))
            .collect();
        if let [Some(new)] = moved.into_iter().collect::<Vec<_>>().as_slice()
            && !old_types.labels.contains_key(new)
        {
            candidates.push((old.clone(), new.clone()));
        }
    }
    candidates
        .iter()
        .filter(|(_, new)| candidates.iter().filter(|(_, n)| n == new).count() == 1)
        .map(|(old, new)| Hint::EnumType {
            old: old.clone(),
            new: new.clone(),
        })
        .collect()
}

fn claim(claims: &mut Vec<(String, Vec<String>)>, previous: &str, claimant: &str) {
    match claims.iter_mut().find(|(name, _)| name == previous) {
        Some((_, claimants)) => claimants.push(claimant.to_string()),
        None => claims.push((previous.to_string(), vec![claimant.to_string()])),
    }
}

/// The `hints` whose renames leave a column's storage on `dialect` as the
/// rest of a plan should see it. On SQLite an enum column is text as wide as
/// its longest label, so a label rename is an `UPDATE` of its rows that can
/// also widen or narrow the column: the label hints are left out, and the
/// plan still meets the width change as the column's type change.
pub(crate) fn storage_hints(hints: &[Hint], dialect: Dialect) -> Vec<Hint> {
    hints
        .iter()
        .filter(|hint| dialect != Dialect::Sqlite || !matches!(hint, Hint::Label { .. }))
        .cloned()
        .collect()
}

/// `hints` run the other way: what turns the new snapshot back into the old
/// one. A down reverses its step's renames with these (ADR-0033).
pub fn reverse_hints(hints: &[Hint]) -> Vec<Hint> {
    let old_table = |table: &str| {
        hints
            .iter()
            .find_map(|hint| match hint {
                Hint::Table { old, new } if new == table => Some(old.clone()),
                _ => None,
            })
            .unwrap_or_else(|| table.to_string())
    };
    hints
        .iter()
        .map(|hint| match hint {
            Hint::Table { old, new } => Hint::Table {
                old: new.clone(),
                new: old.clone(),
            },
            Hint::Column { table, old, new } => Hint::Column {
                table: old_table(table),
                old: new.clone(),
                new: old.clone(),
            },
            Hint::EnumType { old, new } => Hint::EnumType {
                old: new.clone(),
                new: old.clone(),
            },
            // A label hint applies after its type's rename: backwards, after
            // the type is renamed back.
            Hint::Label {
                type_name,
                old,
                new,
            } => Hint::Label {
                type_name: hints
                    .iter()
                    .find_map(|hint| match hint {
                        Hint::EnumType { old, new } if new == type_name => Some(old.clone()),
                        _ => None,
                    })
                    .unwrap_or_else(|| type_name.clone()),
                old: new.clone(),
                new: old.clone(),
            },
        })
        .collect()
}

/// The renames `hints` make, keyed by the old snapshot's names.
struct Renames<'a> {
    hints: &'a [Hint],
}

impl Renames<'_> {
    /// The new name of the old table `table`.
    fn table<'n>(&'n self, table: &'n str) -> &'n str {
        self.hints
            .iter()
            .find_map(|hint| match hint {
                Hint::Table { old, new } if old == table => Some(new.as_str()),
                _ => None,
            })
            .unwrap_or(table)
    }

    /// The new name of column `column` of the old table `table`.
    fn column<'n>(&'n self, table: &str, column: &'n str) -> &'n str {
        let new_table = self.table(table);
        self.hints
            .iter()
            .find_map(|hint| match hint {
                Hint::Column { table: t, old, new } if t == new_table && old == column => {
                    Some(new.as_str())
                }
                _ => None,
            })
            .unwrap_or(column)
    }

    /// `col` of the old snapshot with its enum type renamed (a native type:
    /// the only kind that exists as an object), then its labels, how ever
    /// they are stored (a label hint names the type as its type rename leaves
    /// it). Each label keeps its position, so the label order is unchanged.
    /// Returns the `(old, new)` labels renamed.
    fn enum_column(&self, col: &mut ferro_schema_ir::SchemaColumn) -> Vec<(String, String)> {
        let mut renamed = Vec::new();
        let Some((mut type_name, _)) = enum_declaration(col) else {
            return renamed;
        };
        if enum_type_of(col).is_some() {
            for hint in self.hints {
                match hint {
                    Hint::EnumType { old, new } if *old == type_name => {
                        col.enum_type_name = Some(new.clone());
                        type_name = new.clone();
                        break;
                    }
                    _ => {}
                }
            }
        }
        let Some(values) = col.enum_values.as_mut() else {
            return renamed;
        };
        for hint in self.hints {
            let Hint::Label {
                type_name: t,
                old,
                new,
            } = hint
            else {
                continue;
            };
            if *t != type_name {
                continue;
            }
            for value in values.iter_mut() {
                if enum_label_strings(std::slice::from_ref(value)).first() == Some(old) {
                    *value = serde_json::Value::String(new.clone());
                    renamed.push((old.clone(), new.clone()));
                }
            }
        }
        renamed
    }
}

/// A single-column index or unique naming function (`table`, `column`).
type SingleName = fn(&str, &str) -> String;
/// A composite index or unique naming function (`table`, `columns`).
type CompositeName = fn(&str, &[&str]) -> String;

/// `name` re-derived for the renamed table and columns, when the naming
/// function produced it from the old ones; otherwise unchanged.
fn rederived(name: &str, before: String, after: String) -> String {
    if name == before {
        after
    } else {
        name.to_string()
    }
}

fn renamed_check_expr(
    expr: &ferro_schema_ir::CheckExpr,
    column: &dyn Fn(&str) -> String,
) -> ferro_schema_ir::CheckExpr {
    use ferro_schema_ir::{CheckExpr, CheckOperand};
    match expr {
        CheckExpr::And { left, right } => CheckExpr::And {
            left: Box::new(renamed_check_expr(left, column)),
            right: Box::new(renamed_check_expr(right, column)),
        },
        CheckExpr::Or { left, right } => CheckExpr::Or {
            left: Box::new(renamed_check_expr(left, column)),
            right: Box::new(renamed_check_expr(right, column)),
        },
        CheckExpr::Not { child } => CheckExpr::Not {
            child: Box::new(renamed_check_expr(child, column)),
        },
        CheckExpr::IsNull { column: c } => CheckExpr::IsNull { column: column(c) },
        CheckExpr::IsNotNull { column: c } => CheckExpr::IsNotNull { column: column(c) },
        CheckExpr::Cmp {
            column: c,
            op,
            other,
        } => CheckExpr::Cmp {
            column: column(c),
            op: *op,
            other: match other {
                CheckOperand::Column { name } => CheckOperand::Column { name: column(name) },
                literal => literal.clone(),
            },
        },
        CheckExpr::Compare { left, op, right } => CheckExpr::Compare {
            left: renamed_value_expr(left, column),
            op: *op,
            right: renamed_value_expr(right, column),
        },
        CheckExpr::In { column: c, values } => CheckExpr::In {
            column: column(c),
            values: values.clone(),
        },
        CheckExpr::Like { column: c, pattern } => CheckExpr::Like {
            column: column(c),
            pattern: pattern.clone(),
        },
    }
}

/// `model` of the old snapshot as the renames leave it: its table and columns
/// under their new names, every reference to them followed, and every
/// ferro-owned name re-derived by the naming function that made it.
fn renamed_model(model: &SchemaModel, renames: &Renames<'_>) -> SchemaModel {
    use ferro_ddl_lowering::{
        composite_index_name, composite_unique_index_name, db_check_constraint_name, fk_name,
        row_policy_name, row_policy_short_name, single_index_name, single_unique_index_name,
        table_check_constraint_name, table_check_suffix,
    };
    let t_old = model.table_name.as_str();
    let t_new = renames.table(t_old).to_string();
    let col = |name: &str| renames.column(t_old, name).to_string();
    let mut out = model.clone();
    out.table_name = t_new.clone();
    // A join table's model is named for its table.
    if model.model_name == t_old {
        out.model_name = t_new.clone();
    }
    let mut relabelled: BTreeMap<String, Vec<(String, String)>> = BTreeMap::new();
    for column in &mut out.columns {
        column.name = col(&column.name);
        if let Some(expr) = &mut column.generated {
            *expr = renamed_value_expr(expr, &|c| col(c));
        }
        let renamed = renames.enum_column(column);
        if !renamed.is_empty() {
            relabelled.insert(column.name.clone(), renamed);
        }
    }
    for fk in &mut out.foreign_keys {
        let (c_old, to_old) = (fk.column.clone(), fk.to_table.clone());
        fk.column = col(&c_old);
        fk.to_table = renames.table(&to_old).to_string();
        fk.to_column = renames.column(&to_old, &fk.to_column).to_string();
        fk.name = fk.name.as_deref().map(|name| {
            rederived(
                name,
                fk_name(t_old, &c_old, &to_old),
                fk_name(&t_new, &fk.column, &fk.to_table),
            )
        });
    }
    let index_name = |name: &str, cols_old: &[String], cols_new: &[String], unique: bool| {
        let old_refs: Vec<&str> = cols_old.iter().map(String::as_str).collect();
        let new_refs: Vec<&str> = cols_new.iter().map(String::as_str).collect();
        let (single, composite): (SingleName, CompositeName) = if unique {
            (single_unique_index_name, composite_unique_index_name)
        } else {
            (single_index_name, composite_index_name)
        };
        if let ([one_old], [one_new]) = (old_refs.as_slice(), new_refs.as_slice())
            && name == single(t_old, one_old)
        {
            return single(&t_new, one_new);
        }
        rederived(
            name,
            composite(t_old, &old_refs),
            composite(&t_new, &new_refs),
        )
    };
    for index in &mut out.indexes {
        let cols_new: Vec<String> = index.columns.iter().map(|c| col(c)).collect();
        index.name = index_name(&index.name, &index.columns, &cols_new, index.unique);
        index.columns = cols_new;
    }
    for unique in &mut out.uniques {
        let cols_new: Vec<String> = unique.columns.iter().map(|c| col(c)).collect();
        unique.name = index_name(&unique.name, &unique.columns, &cols_new, true);
        unique.columns = cols_new;
    }
    for check in &mut out.checks {
        let c_new = col(&check.column);
        // A `db_check` over a relabelled column names the labels it allows.
        for (old, new) in relabelled.get(&c_new).into_iter().flatten() {
            let (old, new) = (quote_label(old), quote_label(new));
            for value in &mut check.values {
                if *value == old {
                    *value = new.clone();
                }
            }
        }
        check.name = rederived(
            &check.name,
            db_check_constraint_name(t_old, &check.column),
            db_check_constraint_name(&t_new, &c_new),
        );
        check.column = c_new;
    }
    for check in &mut out.table_checks {
        if let Some(suffix) = table_check_suffix(t_old, &check.name) {
            check.name = rederived(
                &check.name,
                table_check_constraint_name(t_old, suffix),
                table_check_constraint_name(&t_new, suffix),
            );
        }
        check.predicate = renamed_check_expr(&check.predicate, &|c| col(c));
    }
    if let Some(declaration) = &mut out.row_security {
        for policy in &mut declaration.policies {
            if let Some(short) = row_policy_short_name(t_old, &policy.name) {
                policy.name = rederived(
                    &policy.name,
                    row_policy_name(t_old, short),
                    row_policy_name(&t_new, short),
                );
            }
            if let ferro_schema_ir::RowPolicyExpr::Setting { column, .. } = &mut policy.expr {
                *column = col(column);
            }
        }
    }
    out
}

/// `old` as `hints` leave it: what the database holds once the renames have
/// run, and so the side every other change of the migration is planned
/// against. Models stay in `old`'s order, one for one.
pub(crate) fn renamed_snapshot(
    old: &IrEnvelope<SchemaIrPayload>,
    hints: &[Hint],
) -> IrEnvelope<SchemaIrPayload> {
    let renames = Renames { hints };
    let mut out = old.clone();
    out.payload.models = old
        .payload
        .models
        .iter()
        .map(|model| renamed_model(model, &renames))
        .collect();
    out
}

/// The rename ops that turn `old` into `renamed` (its [`renamed_snapshot`]),
/// table by table — the renamed tables first, the tables that reference them
/// after — each table's as one contiguous unit: its table rename, its column
/// renames, then its derived names (indexes, constraints, Postgres policies).
/// The reconciliation pass runs each table's consecutive ops in one
/// transaction; a generated step runs every table and column rename first.
pub(crate) fn rename_ops(
    old: &IrEnvelope<SchemaIrPayload>,
    renamed: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Vec<MigrationOp> {
    let mut pairs: Vec<(&SchemaModel, &SchemaModel)> = old
        .payload
        .models
        .iter()
        .zip(&renamed.payload.models)
        .collect();
    let owns_a_rename = |(o, r): &(&SchemaModel, &SchemaModel)| {
        o.table_name != r.table_name
            || o.columns
                .iter()
                .zip(&r.columns)
                .any(|(a, b)| a.name != b.name)
    };
    // Stable: the renamed tables' own names first, then referencing tables'.
    pairs.sort_by_key(|pair| !owns_a_rename(pair));

    let mut out = Vec::new();
    for (o, r) in pairs {
        let table = r.table_name.clone();
        if o.table_name != r.table_name {
            out.push(MigrationOp::RenameTable {
                old: o.table_name.clone(),
                new: table.clone(),
            });
        }
        for (a, b) in o.columns.iter().zip(&r.columns) {
            if a.name != b.name {
                out.push(MigrationOp::RenameColumn {
                    table: table.clone(),
                    old: a.name.clone(),
                    new: b.name.clone(),
                });
            }
        }
        for ((a, _, _), (b, _, _)) in emit::standalone_indexes(o)
            .into_iter()
            .zip(emit::standalone_indexes(r))
        {
            if a != b {
                out.push(MigrationOp::RenameIndex {
                    table: table.clone(),
                    old: a,
                    new: b,
                });
            }
        }
        let fk_names = |m: &SchemaModel| -> Vec<String> {
            m.foreign_keys
                .iter()
                .map(|fk| emit::fk_constraint_name(&m.table_name, fk))
                .collect()
        };
        let check_names = |m: &SchemaModel| -> Vec<String> {
            m.checks
                .iter()
                .map(|c| c.name.clone())
                .chain(m.table_checks.iter().map(|c| c.name.clone()))
                .collect()
        };
        for (a, b) in fk_names(o)
            .into_iter()
            .zip(fk_names(r))
            .chain(check_names(o).into_iter().zip(check_names(r)))
        {
            if a != b {
                out.push(MigrationOp::RenameConstraint {
                    table: table.clone(),
                    old: a,
                    new: b,
                });
            }
        }
        if dialect == Dialect::Postgres
            && let (Some(a), Some(b)) = (&o.row_security, &r.row_security)
        {
            for (pa, pb) in a.policies.iter().zip(&b.policies) {
                if pa.name != pb.name {
                    out.push(MigrationOp::RenamePolicy {
                        table: table.clone(),
                        old: pa.name.clone(),
                        new: pb.name.clone(),
                    });
                }
            }
        }
    }
    out.extend(enum_rename_ops(old, renamed, dialect));
    out
}

/// The enum renames that turn `old` into `renamed`, after every table's unit
/// (a SQLite label `UPDATE` names its tables and columns as those renames
/// leave them): each type rename (Postgres only — SQLite has no enum types),
/// then each label rename, over every column of its type.
fn enum_rename_ops(
    old: &IrEnvelope<SchemaIrPayload>,
    renamed: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Vec<MigrationOp> {
    let declaring = declared_enum_labels(&renamed.payload.models).declaring;
    let mut types = Vec::new();
    let mut labels = Vec::new();
    let columns = old
        .payload
        .models
        .iter()
        .zip(&renamed.payload.models)
        .flat_map(|(o, r)| o.columns.iter().zip(&r.columns));
    for (a, b) in columns {
        let (Some((type_a, labels_a)), Some((type_b, labels_b))) =
            (enum_declaration(a), enum_declaration(b))
        else {
            continue;
        };
        if type_a != type_b && dialect == Dialect::Postgres && enum_type_of(a).is_some() {
            let op = MigrationOp::RenameEnumType {
                old: type_a,
                new: type_b.clone(),
            };
            if !types.contains(&op) {
                types.push(op);
            }
        }
        for (label_a, label_b) in labels_a.into_iter().zip(labels_b) {
            if label_a == label_b {
                continue;
            }
            let op = MigrationOp::RenameEnumLabel {
                type_name: type_b.clone(),
                old: label_a,
                new: label_b,
                columns: declaring.get(&type_b).cloned().unwrap_or_default(),
            };
            if !labels.contains(&op) {
                labels.push(op);
            }
        }
    }
    types.extend(labels);
    types
}

pub(crate) fn index_models(models: &[SchemaModel]) -> BTreeMap<String, &SchemaModel> {
    models
        .iter()
        .map(|model| (model.table_name.clone(), model))
        .collect()
}

/// The table's primary-key columns, in column order.
fn primary_key_columns(model: &SchemaModel) -> Vec<String> {
    model
        .columns
        .iter()
        .filter(|col| col.primary_key)
        .map(|col| col.name.clone())
        .collect()
}

/// A [`MigrationOp::ChangePrimaryKey`] when the two models' primary keys are
/// not the same set of columns. Compared as sets: a live table reports its
/// columns in catalog order, a declared one in model order.
fn primary_key_change(
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
) -> Option<MigrationOp> {
    let (from, to) = (
        primary_key_columns(old_model),
        primary_key_columns(new_model),
    );
    let set = |cols: &[String]| cols.iter().cloned().collect::<BTreeSet<_>>();
    (set(&from) != set(&to)).then(|| MigrationOp::ChangePrimaryKey {
        table: table.to_string(),
        from,
        to,
    })
}

/// The riders ([`emit::column_riders`]) of every column `new_model` adds to
/// `old_model`: what each `AddColumn` creates, which the planner plans no
/// separate op for.
fn added_column_riders<'a>(
    old_model: &SchemaModel,
    new_model: &'a SchemaModel,
) -> Vec<emit::Riders<'a>> {
    new_model
        .columns
        .iter()
        .filter(|col| !old_model.columns.iter().any(|old| old.name == col.name))
        .map(|col| emit::column_riders(new_model, &col.name))
        .collect()
}

/// Whether a column both sides hold changes its storage type from `old_col`
/// to `new_col` on `dialect`: an [`MigrationOp::AlterColumnType`].
fn type_changes(
    old_col: &ferro_schema_ir::SchemaColumn,
    new_col: &ferro_schema_ir::SchemaColumn,
    dialect: Dialect,
) -> bool {
    // A column declared with one native enum type on both sides that moved
    // to another is a type change too: the storage decision reads a live
    // native-enum column as already at any enum target, and only a
    // declared `old` names its type. A move of every column of a type is
    // its rename, applied before this diff runs (ADR-0032). So is a column
    // that leaves its native type for a scalar storage, which the storage
    // decision also reads as no change (a native enum's scalar cascade is
    // the string's).
    let moved_enum = dialect == Dialect::Postgres
        && matches!(
            (enum_type_of(old_col), enum_type_of(new_col)),
            (Some((a, _)), Some((b, _))) if a != b
        )
        || dialect == Dialect::Postgres
            && enum_type_of(old_col).is_some()
            && enum_type_of(new_col).is_none();
    moved_enum || schema_columns_storage_drift(old_col, new_col, dialect)
}

/// Why the plan drops a stored generated column and adds it back, decided
/// once ([`replace_of`]) and carried on both ops' verdicts, so every door
/// says the same of the pair.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Replace {
    /// The column was plain (or `VIRTUAL`) and is made stored generated: the
    /// add replaces the values its rows held, so only a destructive plan
    /// does it.
    MadeGenerated,
    /// A column its expression reads changes type, which Postgres refuses to
    /// alter while an expression reads it. The add computes every row again.
    InputType,
    /// Its own type changes, which Postgres refuses to alter on a generated
    /// column. The add computes every row again.
    OwnType,
}

/// Why `column`, a stored generated column of `new_model` (on `new`) that
/// `old_model` (on `old`) holds too, is dropped and added back, if it is.
/// Each side answers for its own generation (a live one from its catalog
/// facts). On SQLite the drop and add fold into the one rebuild the change
/// needs, so both dialects plan the same ops (and the same index steps).
fn replace_of(
    old: &Side,
    new: &Side,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    column: &str,
    dialect: Dialect,
) -> Option<Replace> {
    let old_column = |name: &str| old_model.columns.iter().find(|col| col.name == name);
    let new_column = |name: &str| new_model.columns.iter().find(|col| col.name == name);
    let (was_col, now_col) = (old_column(column)?, new_column(column)?);
    let now = new.generation(new_model, column);
    if !now.is_stored() {
        return None;
    }
    let was = old.generation(old_model, column);
    if !was.is_stored() {
        return Some(Replace::MadeGenerated);
    }
    if type_changes(was_col, now_col, dialect) {
        return Some(Replace::OwnType);
    }
    // The columns it reads, from the side that declares it.
    let ((Generation::Declared(expr), _) | (_, Generation::Declared(expr))) = (now, was) else {
        return None;
    };
    ferro_ddl_lowering::value_expr_columns(&expr)
        .iter()
        .any(|input| match (old_column(input), new_column(input)) {
            (Some(was), Some(now)) => type_changes(was, now, dialect),
            _ => false,
        })
        .then_some(Replace::InputType)
}

/// The stored generated columns of `new_model` (on `new`) that
/// `old_model` (on `old`) holds too and the plan drops and adds back
/// ([`replace_of`]), in `new_model`'s order. One made generated from a plain
/// column only a `destructive` plan replaces.
fn replaced_generated_columns(
    old: &Side,
    new: &Side,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    dialect: Dialect,
    destructive: bool,
) -> Vec<String> {
    new_model
        .columns
        .iter()
        .filter(
            |col| match replace_of(old, new, old_model, new_model, &col.name, dialect) {
                Some(Replace::MadeGenerated) => destructive,
                Some(_) => true,
                None => false,
            },
        )
        .map(|col| col.name.clone())
        .collect()
}

/// Whether the plan carries `policy` (as `before` holds it on the table)
/// around the drop and add back of `column`: drops it before the drop and
/// adds it back after the add, from `declaration` (the target's). A
/// shorthand the target declares over `column`; a raw one whose body reads
/// `column` ([`policy_reads`]) when both of its texts are ferro's own copies
/// (a declared `before`) or the catalog's (a live `target`). From a live
/// database toward a declaration a raw body is never rewritten (ADR-0019).
fn carries_policy(
    before: &Side,
    target: &Side,
    declaration: &SchemaModel,
    policy: &LiveRowPolicy,
    column: &str,
) -> bool {
    let Some(declared) = declaration
        .row_security
        .as_ref()
        .and_then(|security| security.policies.iter().find(|p| p.name == policy.name))
    else {
        return false;
    };
    match &declared.expr {
        ferro_schema_ir::RowPolicyExpr::Setting { column: on, .. } => on == column,
        ferro_schema_ir::RowPolicyExpr::Raw { .. } => {
            policy_reads(policy, column)
                && (before.rebuilds_unverifiable_bodies() || target.reports_conditions())
        }
    }
}

/// Whether `policy`'s body reads `column` ([`body_names_column`]).
fn policy_reads(policy: &LiveRowPolicy, column: &str) -> bool {
    [&policy.using, &policy.with_check]
        .into_iter()
        .flatten()
        .any(|body| body_names_column(body, column))
}

/// Whether the policy body `body` names `column` as a word, its quoted
/// string literals left out: the conservative read of a body ferro does not
/// parse (a raw or foreign one), where every word it names may be a column.
fn body_names_column(body: &str, column: &str) -> bool {
    let mut quoted = false;
    let words: String = body
        .chars()
        .map(|ch| {
            // `''` inside a literal closes and reopens it: still quoted.
            if ch == '\'' {
                quoted = !quoted;
                ' '
            } else if quoted {
                ' '
            } else {
                ch
            }
        })
        .collect();
    words
        .split(|ch: char| !(ch.is_alphanumeric() || ch == '_' || ch == '$'))
        .any(|word| word.eq_ignore_ascii_case(column))
}

/// The first row policy on `table` that `before` holds whose body reads
/// `column` ([`policy_reads`]) and that the plan cannot carry around the
/// column's drop and add ([`carries_policy`]): Postgres refuses to drop a
/// column a policy reads.
fn policy_blocking_replace(
    before: &Side,
    target: &Side,
    table: &str,
    column: &str,
) -> Option<String> {
    let was = before.model(table)?;
    let declaration = target.model(table).map(|model| target.declaration(model));
    before
        .table_facts(was)
        .row_security
        .policies
        .iter()
        .find(|policy| {
            policy_reads(policy, column)
                && !declaration.as_ref().is_some_and(|declaration| {
                    carries_policy(before, target, declaration, policy, column)
                })
        })
        .map(|policy| policy.name.clone())
}

/// The table checks of `model` whose body reads any of `columns`.
fn checks_reading(model: &SchemaModel, columns: &[String]) -> Vec<String> {
    model
        .table_checks
        .iter()
        .filter(|check| {
            ferro_ddl_lowering::check_expr_columns(&check.predicate)
                .iter()
                .any(|column| columns.contains(column))
        })
        .map(|check| check.name.clone())
        .collect()
}

/// `model` without `columns` and the indexes, uniques, column checks and
/// foreign keys over any of them: the table once the plan has dropped them.
fn without_columns(model: &SchemaModel, columns: &[String]) -> SchemaModel {
    let gone = |name: &String| columns.contains(name);
    let mut out = model.clone();
    out.columns.retain(|col| !gone(&col.name));
    out.indexes.retain(|index| !index.columns.iter().any(gone));
    out.uniques
        .retain(|unique| !unique.columns.iter().any(gone));
    out.checks.retain(|check| !gone(&check.column));
    out.foreign_keys.retain(|fk| !gone(&fk.column));
    out
}

fn diff_model_columns(
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    dialect: Dialect,
    ops: &mut Vec<MigrationOp>,
    column_drops: &mut Vec<MigrationOp>,
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

    for col in new_cols.keys().filter(|name| !old_cols.contains_key(*name)) {
        ops.push(MigrationOp::AddColumn {
            table: table.to_string(),
            column: (*col).to_string(),
        });
    }
    for col in old_cols.keys().filter(|name| !new_cols.contains_key(*name)) {
        column_drops.push(MigrationOp::DropColumn {
            table: table.to_string(),
            column: (*col).to_string(),
        });
    }
    for (name, new_col) in &new_cols {
        let Some(old_col) = old_cols.get(name) else {
            continue;
        };
        if type_changes(old_col, new_col, dialect) {
            ops.push(MigrationOp::AlterColumnType {
                table: table.to_string(),
                column: (*name).to_string(),
            });
        }
        if old_col.nullable != new_col.nullable {
            ops.push(MigrationOp::AlterColumnNullability {
                table: table.to_string(),
                column: (*name).to_string(),
            });
        }
    }
}

/// `expr` with every column reference renamed by `rename`.
pub fn renamed_value_expr(
    expr: &ferro_schema_ir::ValueExpr,
    rename: &dyn Fn(&str) -> String,
) -> ferro_schema_ir::ValueExpr {
    use ferro_schema_ir::ValueExpr;
    match expr {
        ValueExpr::Column { name } => ValueExpr::Column { name: rename(name) },
        ValueExpr::Literal { .. } => expr.clone(),
        ValueExpr::Binary { op, left, right } => ValueExpr::Binary {
            op: *op,
            left: Box::new(renamed_value_expr(left, rename)),
            right: Box::new(renamed_value_expr(right, rename)),
        },
        ValueExpr::Negate { operand } => ValueExpr::Negate {
            operand: Box::new(renamed_value_expr(operand, rename)),
        },
        ValueExpr::Func { name, args } => ValueExpr::Func {
            name: name.clone(),
            args: args
                .iter()
                .map(|arg| renamed_value_expr(arg, rename))
                .collect(),
        },
    }
}

/// `text` with every run of whitespace one space, trimmed: how SQLite's
/// stored text of a generated column is compared with ferro's rendering.
fn fold_whitespace(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// How one side holds a column's generation.
enum Generation {
    /// Not generated.
    Plain,
    /// Declared generated, with its expression.
    Declared(ferro_schema_ir::ValueExpr),
    /// Generated on a live database, as the catalog reports it.
    Live(LiveGeneratedFact),
}

impl Generation {
    /// Whether the column is a stored generated one: declared generated
    /// (ferro declares only `STORED`), or live with `stored` set.
    fn is_stored(&self) -> bool {
        match self {
            Generation::Plain => false,
            Generation::Declared(_) => true,
            Generation::Live(fact) => fact.stored,
        }
    }
}

impl Side {
    /// How the side holds `column` of `model`'s generation: a declaration's
    /// expression, or a live column's catalog fact.
    fn generation(&self, model: &SchemaModel, column: &str) -> Generation {
        match &self.0 {
            SideKind::Declared(_) => model
                .columns
                .iter()
                .find(|col| col.name == column)
                .and_then(|col| col.generated.clone())
                .map_or(Generation::Plain, Generation::Declared),
            SideKind::Live { .. } => self
                .table_facts(model)
                .generated
                .iter()
                .find(|fact| fact.column == column)
                .cloned()
                .map_or(Generation::Plain, Generation::Live),
        }
    }

    /// Whether the side holds `column` of `model` as a stored generated
    /// column: declared generated, or so in a live catalog.
    pub(crate) fn holds_generated(&self, model: &SchemaModel, column: &str) -> bool {
        self.generation(model, column).is_stored()
    }

    /// The expression `column` of `model` is generated by, as `SET
    /// EXPRESSION` writes it on `dialect`: a declaration's rendering, or a
    /// live column's catalog text, put back as the catalog printed it.
    /// `None` for a plain column.
    pub(crate) fn generated_body(
        &self,
        model: &SchemaModel,
        column: &str,
        dialect: Dialect,
    ) -> Result<Option<String>, String> {
        match self.generation(model, column) {
            Generation::Plain => Ok(None),
            Generation::Declared(expr) => {
                ferro_ddl_lowering::render_value_expr(&expr, dialect, &model.columns).map(Some)
            }
            Generation::Live(fact) => Ok(Some(fact.catalog)),
        }
    }

    /// Refuse a live side whose Postgres generated columns lack a printed
    /// pair the live read owed it (ADR-0060): every live stored generated
    /// column that [`printed_pair_column`] gives a pair, the one rule the
    /// live read prints them by. A column whose input is not live yet gets
    /// none, and the plan adds or changes it instead. A declared side, and
    /// SQLite, need none.
    ///
    /// The declared table is matched to its live table by name first, then
    /// by its rename hint, as the live read matches them.
    ///
    /// # Errors
    /// [`PlanError::MissingPrintedPair`] naming the first such column.
    pub fn check_printed_pairs(
        &self,
        declared: &IrEnvelope<SchemaIrPayload>,
        dialect: Dialect,
    ) -> Result<(), PlanError> {
        let SideKind::Live { ir, facts } = &self.0 else {
            return Ok(());
        };
        if dialect != Dialect::Postgres {
            return Ok(());
        }
        for model in &declared.payload.models {
            let live_table = [Some(&model.table_name), model.renamed_from.as_ref()]
                .into_iter()
                .flatten()
                .find(|table| facts.tables.contains_key(*table));
            let Some(live_table) = live_table else {
                continue;
            };
            let live_columns: &[ferro_schema_ir::SchemaColumn] = ir
                .payload
                .models
                .iter()
                .find(|live| &live.table_name == live_table)
                .map_or(&[], |live| live.columns.as_slice());
            let live = |name: &str| live_columns.iter().find(|col| col.name == name);
            for col in &model.columns {
                let Some(live_name) = printed_pair_column(model, col, &live) else {
                    continue;
                };
                let live_name = &live_name;
                let missing = facts.tables[live_table]
                    .generated
                    .iter()
                    .any(|fact| &fact.column == live_name && fact.stored && fact.printed.is_none());
                if missing {
                    return Err(PlanError::MissingPrintedPair {
                        table: live_table.clone(),
                        column: live_name.clone(),
                    });
                }
            }
            let live_checks = &facts.tables[live_table].checks;
            let live_check = |name: &str| {
                live_checks
                    .iter()
                    .any(|check| check.name == name && check.ferro_owned)
            };
            for (name, _) in printed_pair_checks(model, live_table, &live, &live_check) {
                let missing = live_checks.iter().any(|check| {
                    check.name == name && check.ferro_owned && check.printed.is_none()
                });
                if missing {
                    return Err(PlanError::MissingCheckPrintedPair {
                        table: live_table.clone(),
                        check: name,
                    });
                }
            }
        }
        Ok(())
    }
}

/// The checks of `model` whose bodies the live read prints on Postgres
/// beside the catalog's (ADR-0060), as `(live name, declared body)`: every
/// check `model` declares whose every column is live (`live`, by name or
/// rename hint) and that a live ferro-owned check holds (`live_check`), by
/// its own name or the name a rename of the same change re-derives it from,
/// its body rendered for Postgres under the live table `live_table`'s
/// column names. The live read pairs each with that live check, and
/// [`Side::check_printed_pairs`] demands a pair by the same rule. A check
/// reading a column that is not live yet gets none: its live namesake, if
/// any, reads something else and is rebuilt.
///
/// A body that does not render is its `Err`, naming the check.
pub fn printed_pair_checks<'a>(
    model: &SchemaModel,
    live_table: &str,
    live: &dyn Fn(&str) -> Option<&'a ferro_schema_ir::SchemaColumn>,
    live_check: &dyn Fn(&str) -> bool,
) -> Vec<(String, Result<String, String>)> {
    let present = |name: &str| live(name).is_some();
    let hints: Vec<Hint> = std::iter::once(Hint::Table {
        old: model.table_name.clone(),
        new: live_table.to_string(),
    })
    .chain(model.columns.iter().filter_map(|col| {
        let name = live_column_name(model, &col.name, &present)?;
        (name != col.name).then(|| Hint::Column {
            table: live_table.to_string(),
            old: col.name.clone(),
            new: name,
        })
    }))
    .collect();
    let view = renamed_model(model, &Renames { hints: &hints });
    let table_checks = view.table_checks.iter().map(|check| {
        (
            &check.name,
            ferro_ddl_lowering::check_expr_columns(&check.predicate),
        )
    });
    let column_checks = view
        .checks
        .iter()
        .map(|check| (&check.name, BTreeSet::from([check.column.clone()])));
    // `renamed_model` keeps the checks in order, one for one.
    declared_check_names(model)
        .into_iter()
        .zip(table_checks.chain(column_checks))
        // Every column live and stored as declared, under its live name.
        .filter(|(_, (_, columns))| {
            columns.iter().all(|column| {
                live(column).is_some_and(|live_col| {
                    view.columns
                        .iter()
                        .find(|col| &col.name == column)
                        .is_some_and(|col| !type_changes(live_col, col, Dialect::Postgres))
                })
            })
        })
        .filter_map(|(declared, (rederived, _))| {
            let name = [declared.as_str(), rederived.as_str()]
                .into_iter()
                .find(|name| live_check(name))?
                .to_string();
            let body = declared_check_body(&view, rederived, Dialect::Postgres).and_then(|body| {
                body.ok_or_else(|| format!("CHECK constraint '{rederived}' is not declared"))
            });
            Some((name, body))
        })
        .collect()
}

/// The live name of the declared column `name` of `model`: its own name
/// when the live table holds it (`live`), else its rename hint's.
pub fn live_column_name(
    model: &SchemaModel,
    name: &str,
    live: &dyn Fn(&str) -> bool,
) -> Option<String> {
    let col = model.columns.iter().find(|col| col.name == name)?;
    [Some(&col.name), col.renamed_from.as_ref()]
        .into_iter()
        .flatten()
        .find(|name| live(name))
        .cloned()
}

/// Whether the declared column `name` of `model` is live (`live`, the live
/// column of a live name), by name or rename hint, stored as `model`
/// declares it: an expression or check over it the live read renders then
/// reads the live table as the declaration does. One whose type changes may
/// not type-check against the live table, and Postgres refuses to print it.
fn live_as_declared<'a>(
    model: &SchemaModel,
    name: &str,
    live: &dyn Fn(&str) -> Option<&'a ferro_schema_ir::SchemaColumn>,
) -> bool {
    let Some(declared) = model.columns.iter().find(|col| col.name == name) else {
        return false;
    };
    [Some(&declared.name), declared.renamed_from.as_ref()]
        .into_iter()
        .flatten()
        .find_map(|name| live(name))
        .is_some_and(|live_col| !type_changes(live_col, declared, Dialect::Postgres))
}

/// The live name of `col` when the live read prints its pair on Postgres
/// (ADR-0060): `col` is generated, and it and every column its expression
/// reads are live (`live`, the live column of a live name), by name or
/// rename hint, each stored as declared ([`live_as_declared`]). The one
/// rule the live read prints pairs by and [`Side::check_printed_pairs`]
/// demands them by. A declared column or input absent live, or changing
/// type, gets no pair: the plan adds the column, changes its expression, or
/// drops and adds it back, instead.
pub fn printed_pair_column<'a>(
    model: &SchemaModel,
    col: &ferro_schema_ir::SchemaColumn,
    live: &dyn Fn(&str) -> Option<&'a ferro_schema_ir::SchemaColumn>,
) -> Option<String> {
    let expr = col.generated.as_ref()?;
    let name = live_column_name(model, &col.name, &|name| live(name).is_some())?;
    (live_as_declared(model, &col.name, live)
        && ferro_ddl_lowering::value_expr_columns(expr)
            .iter()
            .all(|input| live_as_declared(model, input, live)))
    .then_some(name)
}

/// Whether `column`'s generation differs between `old_model` on `old` and
/// `new_model` on `new` (ADR-0060). Two declarations compare their
/// expressions. A live column compares with a declaration through what the
/// live read gathered: on Postgres the printed pair (Postgres printed both
/// expressions, so equal strings are one expression), on SQLite the stored
/// text against ferro's rendering, guard included, whitespace folded. A live
/// `VIRTUAL` column, or a plain column on one side and a generated one on
/// the other, always differs.
fn generation_differs(
    old: &Side,
    new: &Side,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    column: &str,
    dialect: Dialect,
) -> bool {
    let live_against = |fact: &LiveGeneratedFact, model: &SchemaModel| {
        if !fact.stored {
            return true;
        }
        match dialect {
            Dialect::Postgres => fact
                .printed
                .as_ref()
                .is_none_or(|pair| pair.declared != pair.catalog),
            Dialect::Sqlite => model
                .columns
                .iter()
                .find(|col| col.name == column)
                .and_then(|col| emit::generated_column_text(model, col, dialect).ok())
                .flatten()
                .is_none_or(|text| fold_whitespace(&text) != fold_whitespace(&fact.catalog)),
        }
    };
    match (
        old.generation(old_model, column),
        new.generation(new_model, column),
    ) {
        (Generation::Plain, Generation::Plain) => false,
        (Generation::Declared(was), Generation::Declared(now)) => was != now,
        (Generation::Live(fact), Generation::Declared(_)) => live_against(&fact, new_model),
        (Generation::Declared(_), Generation::Live(fact)) => live_against(&fact, old_model),
        (Generation::Live(was), Generation::Live(now)) => {
            was.stored != now.stored
                || fold_whitespace(&was.catalog) != fold_whitespace(&now.catalog)
        }
        _ => true,
    }
}

/// The generation changes of every column both models hold: a generated
/// column the target holds plain is a
/// [`MigrationOp::DropGeneratedExpression`]; a stored generated column on
/// both sides whose expression differs ([`generation_differs`]) a
/// [`MigrationOp::AlterGeneratedExpression`]. A column generated on the
/// target only is dropped and added ([`replaced_generated_columns`]).
fn diff_generated(
    old: &Side,
    new: &Side,
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    dialect: Dialect,
    ops: &mut Vec<MigrationOp>,
) {
    for col in &new_model.columns {
        if !old_model.columns.iter().any(|was| was.name == col.name) {
            continue;
        }
        let (table, column) = (table.to_string(), col.name.clone());
        let was = old.generation(old_model, &col.name);
        let now = new.generation(new_model, &col.name);
        if matches!(now, Generation::Plain) && !matches!(was, Generation::Plain) {
            ops.push(MigrationOp::DropGeneratedExpression { table, column });
        } else if now.is_stored()
            && was.is_stored()
            && generation_differs(old, new, old_model, new_model, &col.name, dialect)
        {
            ops.push(MigrationOp::AlterGeneratedExpression { table, column });
        }
    }
}

fn diff_model_indexes(
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    destructive: bool,
    ops: &mut Vec<MigrationOp>,
) {
    let riders = added_column_riders(old_model, new_model);

    // Both sides through `standalone_indexes`: a declared snapshot carries a
    // unique in `uniques`, a live one in `indexes` (introspection leaves
    // `uniques` empty), and either way it is one standalone index.
    let old_by_name: BTreeMap<String, (Vec<String>, bool)> = emit::standalone_indexes(old_model)
        .into_iter()
        .map(|(name, columns, unique)| (name, (columns, unique)))
        .collect();
    let new_set = emit::standalone_indexes(new_model);
    let new_names: BTreeSet<&str> = new_set.iter().map(|(n, _, _)| n.as_str()).collect();

    for (name, columns, unique) in &new_set {
        match old_by_name.get(name) {
            // The same name over other columns, or with the other uniqueness
            // (ADR-0051): a name cut to 63 characters, an underscore join, or
            // a live index written another way. Still declared, so never
            // gated on `destructive`; it also replaces what an added column's
            // `CREATE INDEX IF NOT EXISTS` would leave standing under the name.
            Some((old_columns, old_unique)) => {
                if old_columns != columns || old_unique != unique {
                    ops.push(MigrationOp::RedefineIndex {
                        table: table.to_string(),
                        name: name.clone(),
                    });
                }
            }
            // A rider of an added column is built by its `AddColumn`.
            // Composite indexes never ride a column and are planned even
            // when every indexed column is new (I-1).
            None if riders.iter().any(|riders| riders.has_index(name)) => {}
            None => ops.push(MigrationOp::AddIndex {
                table: table.to_string(),
                name: name.clone(),
                columns: columns.clone(),
                unique: *unique,
            }),
        }
    }
    if destructive {
        for name in old_by_name.keys() {
            if !new_names.contains(name.as_str()) {
                ops.push(MigrationOp::DropIndex {
                    table: table.to_string(),
                    name: name.clone(),
                });
            }
        }
    }
}

fn diff_model_foreign_keys(
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    destructive: bool,
    ops: &mut Vec<MigrationOp>,
    reports: &mut Vec<Report>,
) {
    let riders = added_column_riders(old_model, new_model);

    // A ferro-owned FK on a column both sides keep that the model no longer
    // declares (ADR-0051): removed under ADR-0013's ladder, like every other
    // constraint the model stops declaring. A user-owned one is never
    // touched; one on a dropped column goes with the column. SQLite exposes
    // no live constraint name, so its FK reads as the canonical one.
    if destructive {
        for live in &old_model.foreign_keys {
            let kept = new_model.columns.iter().any(|col| col.name == live.column);
            let declared = new_model
                .foreign_keys
                .iter()
                .any(|fk| fk.column == live.column);
            let name = live
                .name
                .clone()
                .unwrap_or_else(|| fk_name(table, &live.column, &live.to_table));
            if kept && !declared && is_ferro_fk_name(&name) {
                ops.push(MigrationOp::DropForeignKey {
                    table: table.to_string(),
                    column: live.column.clone(),
                    name,
                });
            }
        }
    }

    for fk in &new_model.foreign_keys {
        // An FK on a newly added column rides its `AddColumn`; the reconcile
        // step only governs FKs whose column already exists live.
        if riders.iter().any(|riders| riders.foreign_key == Some(fk)) {
            continue;
        }

        let Some(live) = old_model
            .foreign_keys
            .iter()
            .find(|live| live.column == fk.column)
        else {
            ops.push(MigrationOp::AddForeignKey {
                table: table.to_string(),
                column: fk.column.clone(),
            });
            continue;
        };

        // Live `to_column` can be empty when the backend reports an
        // implicit-PK reference (SQLite); only a stated target can drift.
        let target_drift = live.to_table != fk.to_table
            || (!live.to_column.is_empty() && live.to_column != fk.to_column);
        // Compare via the canonical SQL rendering — sea-query's
        // `ForeignKeyAction` has no equality of its own.
        let action_drift = fk_action_sql(fk_action_from_str(live.on_delete.as_deref()))
            != fk_action_sql(fk_action_from_str(fk.on_delete.as_deref()));
        if !target_drift && !action_drift {
            continue;
        }

        match live.name.as_deref() {
            // A drifting constraint ferro does not own is never altered —
            // but it is never silent either.
            Some(name) if !is_ferro_fk_name(name) => {
                reports.push(foreign_fk_drift_warning(
                    table,
                    &fk.column,
                    name,
                    (
                        &live.to_table,
                        fk_action_sql(fk_action_from_str(live.on_delete.as_deref())),
                    ),
                    (
                        &fk.to_table,
                        fk_action_sql(fk_action_from_str(fk.on_delete.as_deref())),
                    ),
                ));
            }
            _ => {
                ops.push(MigrationOp::RebuildForeignKey {
                    table: table.to_string(),
                    column: fk.column.clone(),
                    // SQLite exposes no live constraint names; fall back to
                    // the canonical name (unused there — emission warns).
                    old_name: live
                        .name
                        .clone()
                        .unwrap_or_else(|| fk_name(table, &fk.column, &live.to_table)),
                });
            }
        }
    }
}

/// Whether `op` is a rename op: a table, column, index, constraint, policy,
/// enum label or enum type rename. The crate's one test — the live facts a
/// plan's renames carry (`renamed_facts`), a down's renames
/// ([`plan_down`]) and the generator's rename steps all read it.
pub fn is_rename(op: &MigrationOp) -> bool {
    matches!(
        op,
        MigrationOp::RenameTable { .. }
            | MigrationOp::RenameColumn { .. }
            | MigrationOp::RenameIndex { .. }
            | MigrationOp::RenameConstraint { .. }
            | MigrationOp::RenamePolicy { .. }
            | MigrationOp::RenameEnumLabel { .. }
            | MigrationOp::RenameEnumType { .. }
    )
}

#[cfg(test)]
mod down_tests {
    //! `plan_down` (ADR-0050): the up's artifacts planned back, from the
    //! models to a live database (the Alembic bridge's `downgrade()`) or
    //! between two declared stages (a generated step's down).
    use super::*;
    use ferro_ddl_lowering::LiveRowPolicy;
    use ferro_schema_ir::{SchemaColumn, SchemaTableCheck};

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
            retired: false,
            generated: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: false,
            enum_renamed_labels: Default::default(),
            enum_retired_labels: Default::default(),
        }
    }

    fn status(labels: &[&str], live: bool) -> SchemaColumn {
        SchemaColumn {
            logical_type: "string".into(),
            db_type: None,
            enum_values: Some(
                labels
                    .iter()
                    .map(|label| serde_json::json!(label))
                    .collect(),
            ),
            enum_type_name: Some("status".into()),
            postgres_native_enum: live,
            ..column("status", "status", false)
        }
    }

    fn card(columns: Vec<SchemaColumn>) -> SchemaModel {
        SchemaModel {
            renamed_from: None,
            model_name: "app.Card".into(),
            table_name: "card".into(),
            columns,
            foreign_keys: Vec::new(),
            indexes: Vec::new(),
            uniques: Vec::new(),
            checks: Vec::new(),
            table_checks: Vec::new(),
            row_security: None,
        }
    }

    fn not_null_check(name: &str, column: &str) -> SchemaTableCheck {
        SchemaTableCheck {
            name: name.into(),
            predicate: serde_json::from_value(serde_json::json!({
                "kind": "is_null", "column": column, "negated": true
            }))
            .expect("predicate"),
        }
    }

    fn live(models: Vec<SchemaModel>, card: LiveTableFacts) -> Side {
        let mut facts = LiveFacts::default();
        facts.tables.insert("card".into(), card);
        Side::live(envelope(models), facts).expect("live side")
    }

    fn declared(models: Vec<SchemaModel>) -> Side {
        Side::declared(envelope(models))
    }

    const DESTRUCTIVE: PlanOptions = PlanOptions { destructive: true };

    /// The up from `before` to `after` and its down, on `dialect`.
    fn up_and_down(
        before: &Side,
        after: &Side,
        dialect: Dialect,
        options: PlanOptions,
    ) -> (Plan, Plan) {
        let up = plan_from_ir(before, after, dialect, options);
        let ops: Vec<MigrationOp> = up.ops().cloned().collect();
        let down = plan_down(&ops, after, before, dialect);
        (up, down)
    }

    fn ops(plan: &Plan) -> Vec<MigrationOp> {
        plan.ops().cloned().collect()
    }

    fn statements(plan: &Plan) -> Vec<Vec<String>> {
        plan.render()
            .expect("renders")
            .into_iter()
            .map(|op| op.statements)
            .collect()
    }

    // -- ADR-0050's four worked cases --------------------------------------

    #[test]
    fn an_extra_live_label_stays_and_the_added_label_cannot_come_off_a_live_type() {
        // Live `status` holds [a, x]; the models declare [a, y].
        let live_card = card(vec![column("id", "int", false), status(&["a", "x"], true)]);
        let mut facts = LiveFacts::default();
        facts
            .tables
            .insert("card".into(), LiveTableFacts::default());
        facts
            .enum_labels
            .insert("status".into(), vec!["a".into(), "x".into()]);
        let live = Side::live(envelope(vec![live_card]), facts).expect("live side");
        let models = declared(vec![card(vec![
            column("id", "int", false),
            status(&["a", "y"], false),
        ])]);
        let (up, down) = up_and_down(&live, &models, Dialect::Postgres, DESTRUCTIVE);
        // The up adds `y` and reports `x`.
        assert_eq!(
            ops(&up),
            vec![MigrationOp::AddEnumLabel {
                type_name: "status".into(),
                label: "y".into()
            }]
        );
        assert!(
            up.reports
                .iter()
                .any(|report| matches!(&report.kind, crate::ReportKind::ExtraEnumLabels { labels } if labels == &["x".to_string()]))
        );
        // Backwards the planner would remove `y` and add `x`: adding `x` is
        // out of scope, and removing `y` is irreversible on a live type.
        let [PlannedOp { op, verdict }] = down.operations.as_slice() else {
            panic!("{:?}", down.operations);
        };
        assert_eq!(
            op,
            &MigrationOp::RemoveEnumLabel {
                type_name: "status".into(),
                label: "y".into(),
                columns: vec![("card".into(), "status".into())],
            }
        );
        let Execution::Irreversible(reason) = &verdict.execution else {
            panic!("{verdict:?}");
        };
        assert_eq!(
            reason,
            "label \"y\" of enum type status cannot be removed: enum labels are append-only \
             (ADR-0011), and rows may hold it"
        );
    }

    fn policy(name: &str, ferro_owned: bool, roles: &[&str]) -> LiveRowPolicy {
        LiveRowPolicy {
            name: name.into(),
            command: "select".into(),
            restrictive: false,
            using: Some("(id > 0)".into()),
            with_check: None,
            roles: roles.iter().map(|role| role.to_string()).collect(),
            ferro_owned,
        }
    }

    #[test]
    fn a_foreign_policy_is_never_touched_and_the_added_one_comes_off() {
        // Live `card` holds the foreign policy `audit`, row security on; the
        // models add `rls_card_owner`.
        let bare = card(vec![column("id", "int", false)]);
        let live = live(
            vec![bare.clone()],
            LiveTableFacts {
                row_security: LiveRowSecurity {
                    enabled: true,
                    forced: false,
                    policies: vec![policy("audit", false, &["public"])],
                },
                ..LiveTableFacts::default()
            },
        );
        let mut guarded = bare;
        guarded.row_security = Some(ferro_schema_ir::SchemaRowSecurity {
            force: false,
            policies: vec![ferro_schema_ir::SchemaRowPolicy {
                name: "rls_card_owner".into(),
                command: ferro_schema_ir::RowPolicyCommand::Select,
                restrictive: false,
                expr: ferro_schema_ir::RowPolicyExpr::Raw {
                    using: Some("id > 0".into()),
                    with_check: None,
                },
            }],
        });
        let models = declared(vec![guarded]);
        let (up, down) = up_and_down(&live, &models, Dialect::Postgres, DESTRUCTIVE);
        assert_eq!(
            ops(&up),
            vec![MigrationOp::AddRowPolicy {
                table: "card".into(),
                name: "rls_card_owner".into()
            }]
        );
        assert_eq!(
            ops(&down),
            vec![MigrationOp::DropRowPolicy {
                table: "card".into(),
                name: "rls_card_owner".into()
            }]
        );
        assert_eq!(
            statements(&down),
            vec![vec![
                "DROP POLICY \"rls_card_owner\" ON \"card\"".to_string()
            ]]
        );
    }

    fn leftover() -> (Side, Side) {
        let bare = card(vec![column("id", "int", false)]);
        let live = live(
            vec![bare.clone()],
            LiveTableFacts {
                checks: vec![LiveCheckFact {
                    name: "ck_card_old".into(),
                    definition: "CHECK ((id > 0))".into(),
                    ferro_owned: true,
                    validated: true,
                    column: None,
                    printed: None,
                }],
                ..LiveTableFacts::default()
            },
        );
        (live, declared(vec![bare]))
    }

    #[test]
    fn a_leftover_check_the_up_left_alone_is_not_restored() {
        let (live, models) = leftover();
        let (up, down) = up_and_down(&live, &models, Dialect::Postgres, PlanOptions::default());
        assert!(up.is_empty(), "{:?}", up.operations);
        assert!(down.is_empty(), "{:?}", down.operations);
    }

    #[test]
    fn a_leftover_check_the_up_dropped_comes_back_with_the_body_the_catalog_printed() {
        let (live, models) = leftover();
        let (up, down) = up_and_down(&live, &models, Dialect::Postgres, DESTRUCTIVE);
        assert_eq!(
            ops(&up),
            vec![MigrationOp::DropCheck {
                table: "card".into(),
                name: "ck_card_old".into()
            }]
        );
        assert_eq!(
            ops(&down),
            vec![MigrationOp::AddCheck {
                table: "card".into(),
                name: "ck_card_old".into()
            }]
        );
        assert_eq!(
            statements(&down),
            vec![vec![
                "ALTER TABLE \"card\" ADD CONSTRAINT \"ck_card_old\" CHECK ((id > 0))".to_string()
            ]]
        );
        // On SQLite a check comes back to an existing table only by a
        // rebuild: the bridge's downgrade makes that irreversible.
        let (_, down) = up_and_down(&live, &models, Dialect::Sqlite, DESTRUCTIVE);
        assert_eq!(down.operations.len(), 1);
        assert_eq!(down.operations[0].verdict.execution, Execution::Rebuild);
    }

    #[test]
    fn a_leftover_check_renamed_with_its_table_and_dropped_comes_back_under_its_old_name() {
        // ADR-0050's fourth case beside a table rename: the models rename
        // `card` to `deck`, and the live `card` holds the leftover
        // `ck_card_old`, which only the live facts carry.
        let (live, _) = leftover();
        let mut deck = card(vec![column("id", "int", false)]);
        deck.table_name = "deck".into();
        deck.model_name = "app.Deck".into();
        deck.renamed_from = Some("card".into());
        let models = declared(vec![deck]);
        let (up, down) = up_and_down(&live, &models, Dialect::Postgres, DESTRUCTIVE);
        assert_eq!(
            ops(&up),
            vec![
                MigrationOp::RenameTable {
                    old: "card".into(),
                    new: "deck".into()
                },
                MigrationOp::RenameConstraint {
                    table: "deck".into(),
                    old: "ck_card_old".into(),
                    new: "ck_deck_old".into()
                },
                MigrationOp::DropCheck {
                    table: "deck".into(),
                    name: "ck_deck_old".into()
                },
            ]
        );
        // The down undoes the up's renames before it compares keys: the
        // check comes back with the catalog's body under its old name.
        assert_eq!(
            ops(&down),
            vec![
                MigrationOp::RenameTable {
                    old: "deck".into(),
                    new: "card".into()
                },
                MigrationOp::AddCheck {
                    table: "card".into(),
                    name: "ck_card_old".into()
                },
            ]
        );
        assert_eq!(
            statements(&down),
            vec![
                vec!["ALTER TABLE \"deck\" RENAME TO \"card\"".to_string()],
                vec![
                    "ALTER TABLE \"card\" ADD CONSTRAINT \"ck_card_old\" CHECK ((id > 0))"
                        .to_string()
                ],
            ]
        );
    }

    // -- the second live-side refusal ----------------------------------------

    #[test]
    fn a_policy_applying_to_a_role_list_cannot_be_put_back_on_a_live_table() {
        let bare = card(vec![column("id", "int", false)]);
        let live = live(
            vec![bare.clone()],
            LiveTableFacts {
                row_security: LiveRowSecurity {
                    enabled: true,
                    forced: false,
                    policies: vec![policy("rls_card_admin", true, &["admin", "auditor"])],
                },
                ..LiveTableFacts::default()
            },
        );
        let mut guarded = bare;
        guarded.row_security = Some(ferro_schema_ir::SchemaRowSecurity {
            force: false,
            policies: Vec::new(),
        });
        let models = declared(vec![guarded]);
        let (up, down) = up_and_down(&live, &models, Dialect::Postgres, DESTRUCTIVE);
        assert_eq!(
            ops(&up),
            vec![MigrationOp::DropRowPolicy {
                table: "card".into(),
                name: "rls_card_admin".into()
            }]
        );
        let [PlannedOp { op, verdict }] = down.operations.as_slice() else {
            panic!("{:?}", down.operations);
        };
        assert_eq!(
            op,
            &MigrationOp::AddRowPolicy {
                table: "card".into(),
                name: "rls_card_admin".into()
            }
        );
        assert_eq!(
            verdict.execution,
            Execution::Irreversible(
                "row policy rls_card_admin on card applies TO admin, auditor, a clause \
                 ferro's CREATE POLICY never writes; restore it by hand"
                    .into()
            )
        );
    }

    // -- the reverse cases, now planned back -------------------------------

    #[test]
    fn an_empty_up_has_an_empty_down() {
        let (live, models) = leftover();
        assert!(plan_down(&[], &models, &live, Dialect::Postgres).is_empty());
    }

    #[test]
    fn an_added_column_and_its_check_come_off_check_first() {
        let bare = card(vec![column("id", "int", false)]);
        let live = live(vec![bare], LiveTableFacts::default());
        let mut flavored = card(vec![
            column("id", "int", false),
            column("flavor", "varchar", true),
        ]);
        flavored
            .table_checks
            .push(not_null_check("ck_card_flavor_set", "flavor"));
        let models = declared(vec![flavored]);
        let (up, down) = up_and_down(&live, &models, Dialect::Postgres, DESTRUCTIVE);
        assert_eq!(
            ops(&up),
            vec![
                MigrationOp::AddColumn {
                    table: "card".into(),
                    column: "flavor".into()
                },
                MigrationOp::AddCheck {
                    table: "card".into(),
                    name: "ck_card_flavor_set".into()
                },
            ]
        );
        assert_eq!(
            statements(&down),
            vec![
                vec!["ALTER TABLE \"card\" DROP CONSTRAINT \"ck_card_flavor_set\"".to_string()],
                vec!["ALTER TABLE \"card\" DROP COLUMN \"flavor\"".to_string()],
            ]
        );
    }

    #[test]
    fn a_re_added_required_column_demands_values_and_is_left_to_the_caller() {
        // Dropping `nickname: str` (NOT NULL, no default): its down re-adds
        // a column existing rows hold no value for. The renderer has no
        // statement for it; the caller leaves it out and writes it.
        let live = live(
            vec![card(vec![
                column("id", "int", false),
                column("nickname", "varchar", false),
                column("bio", "varchar", true),
            ])],
            LiveTableFacts::default(),
        );
        let models = declared(vec![card(vec![column("id", "int", false)])]);
        let (_, down) = up_and_down(&live, &models, Dialect::Postgres, DESTRUCTIVE);
        let nickname = down
            .operations
            .iter()
            .position(|planned| {
                planned.op
                    == MigrationOp::AddColumn {
                        table: "card".into(),
                        column: "nickname".into(),
                    }
            })
            .expect("the re-add is planned");
        let verdict = &down.operations[nickname].verdict;
        assert!(verdict.demands_values && verdict.recreates, "{verdict:?}");
        let err = down
            .render()
            .expect_err("no statement backfills existing rows");
        assert!(err.message.contains("card.nickname"), "{}", err.message);
        let rest: Vec<usize> = (0..down.operations.len())
            .filter(|&index| index != nickname)
            .collect();
        let rendered = down.render_ops(&rest).expect("renders the rest");
        assert_eq!(
            rendered
                .into_iter()
                .map(|op| op.statements)
                .collect::<Vec<_>>(),
            vec![vec![
                "ALTER TABLE \"card\" ADD COLUMN \"bio\" varchar".to_string()
            ]]
        );
    }

    #[test]
    fn a_rebuilt_check_comes_back_with_its_catalog_body() {
        let bare = card(vec![column("id", "int", false)]);
        let live = live(
            vec![bare.clone()],
            LiveTableFacts {
                checks: vec![LiveCheckFact {
                    name: "ck_card_legacy".into(),
                    definition: "CHECK ((id > 0))".into(),
                    ferro_owned: true,
                    validated: true,
                    column: None,
                    printed: None,
                }],
                ..LiveTableFacts::default()
            },
        );
        let mut checked = bare;
        checked
            .table_checks
            .push(not_null_check("ck_card_legacy", "id"));
        let models = declared(vec![checked]);
        let (up, down) = up_and_down(&live, &models, Dialect::Postgres, DESTRUCTIVE);
        assert_eq!(
            ops(&up),
            vec![MigrationOp::RebuildCheck {
                table: "card".into(),
                name: "ck_card_legacy".into()
            }]
        );
        assert_eq!(
            statements(&down),
            vec![vec![
                "ALTER TABLE \"card\" DROP CONSTRAINT \"ck_card_legacy\"".to_string(),
                "ALTER TABLE \"card\" ADD CONSTRAINT \"ck_card_legacy\" CHECK ((id > 0))"
                    .to_string()
            ]]
        );
    }

    #[test]
    fn a_rebuilt_policy_comes_back_as_the_catalog_printed_it() {
        let bare = card(vec![column("id", "int", false)]);
        let live = live(
            vec![bare.clone()],
            LiveTableFacts {
                row_security: LiveRowSecurity {
                    enabled: true,
                    forced: false,
                    policies: vec![policy("rls_card_mine", true, &["public"])],
                },
                ..LiveTableFacts::default()
            },
        );
        // The models write the policy for every command: a drift the up
        // rebuilds.
        let mut guarded = bare;
        guarded.row_security = Some(ferro_schema_ir::SchemaRowSecurity {
            force: false,
            policies: vec![ferro_schema_ir::SchemaRowPolicy {
                name: "rls_card_mine".into(),
                command: ferro_schema_ir::RowPolicyCommand::All,
                restrictive: false,
                expr: ferro_schema_ir::RowPolicyExpr::Raw {
                    using: Some("(id > 0)".into()),
                    with_check: None,
                },
            }],
        });
        let models = declared(vec![guarded]);
        let (up, down) = up_and_down(&live, &models, Dialect::Postgres, DESTRUCTIVE);
        assert_eq!(
            ops(&up),
            vec![MigrationOp::RebuildRowPolicy {
                table: "card".into(),
                name: "rls_card_mine".into()
            }]
        );
        assert_eq!(
            statements(&down),
            vec![vec![
                "DROP POLICY \"rls_card_mine\" ON \"card\"".to_string(),
                "CREATE POLICY \"rls_card_mine\" ON \"card\" FOR SELECT USING ((id > 0))"
                    .to_string()
            ]]
        );
    }

    #[test]
    fn renames_run_back_first_the_other_way_round() {
        let live = live(
            vec![card(vec![
                column("id", "int", false),
                column("label", "varchar", true),
            ])],
            LiveTableFacts::default(),
        );
        let models = declared(vec![card(vec![
            column("id", "int", false),
            SchemaColumn {
                renamed_from: Some("label".into()),
                ..column("title", "varchar", true)
            },
            column("note", "varchar", true),
        ])]);
        let (_, down) = up_and_down(&live, &models, Dialect::Postgres, DESTRUCTIVE);
        assert_eq!(
            statements(&down),
            vec![
                vec!["ALTER TABLE \"card\" RENAME COLUMN \"title\" TO \"label\"".to_string()],
                vec!["ALTER TABLE \"card\" DROP COLUMN \"note\"".to_string()],
            ]
        );
    }

    // -- between two declared stages ---------------------------------------

    #[test]
    fn a_down_between_two_declared_stages_is_never_irreversible() {
        // Every shape a live target refuses, between two declarations: an
        // added label (its down removes it), a dropped policy applying to no
        // role list (a declaration writes none), a dropped and a rebuilt
        // check, a dropped column and table.
        let mut base = card(vec![column("id", "int", false), status(&["a"], false)]);
        base.table_checks.push(not_null_check("ck_card_old", "id"));
        base.row_security = Some(ferro_schema_ir::SchemaRowSecurity {
            force: true,
            policies: vec![ferro_schema_ir::SchemaRowPolicy {
                name: "rls_card_mine".into(),
                command: ferro_schema_ir::RowPolicyCommand::Select,
                restrictive: false,
                expr: ferro_schema_ir::RowPolicyExpr::Raw {
                    using: Some("id > 0".into()),
                    with_check: None,
                },
            }],
        });
        let mut other = card(vec![column("id", "int", false)]);
        other.table_name = "tag".into();
        other.model_name = "app.Tag".into();
        let before = vec![base.clone(), other];
        let mut edited = card(vec![
            column("id", "int", false),
            status(&["a", "b"], false),
            column("note", "varchar", false),
        ]);
        edited.row_security = Some(ferro_schema_ir::SchemaRowSecurity {
            force: false,
            policies: Vec::new(),
        });
        for dialect in [Dialect::Postgres, Dialect::Sqlite] {
            for (old, new) in [
                (before.clone(), vec![edited.clone()]),
                (vec![edited.clone()], before.clone()),
            ] {
                let (up, down) = up_and_down(&declared(old), &declared(new), dialect, DESTRUCTIVE);
                assert!(!down.is_empty() && !up.is_empty());
                assert!(
                    down.operations.iter().all(|planned| !matches!(
                        planned.verdict.execution,
                        Execution::Irreversible(_)
                    )),
                    "{dialect:?} {:?}",
                    down.operations
                );
            }
        }
    }
}

#[cfg(test)]
mod live_table_hint_tests {
    use super::*;

    fn model(table: &str, renamed_from: Option<&str>) -> SchemaModel {
        SchemaModel {
            renamed_from: renamed_from.map(str::to_string),
            model_name: format!("app.{table}"),
            table_name: table.into(),
            columns: Vec::new(),
            foreign_keys: Vec::new(),
            indexes: Vec::new(),
            uniques: Vec::new(),
            checks: Vec::new(),
            table_checks: Vec::new(),
            row_security: None,
        }
    }

    fn declared(models: Vec<SchemaModel>) -> SchemaIrPayload {
        SchemaIrPayload {
            dialect_agnostic: true,
            models,
        }
    }

    fn live(tables: &[&str]) -> BTreeSet<String> {
        tables.iter().map(|t| t.to_string()).collect()
    }

    #[test]
    fn a_hint_is_live_only_while_the_old_table_stands_alone() {
        let new = declared(vec![model("author", Some("writer"))]);
        let renamed = Hint::Table {
            old: "writer".into(),
            new: "author".into(),
        };
        assert_eq!(
            live_table_hints(&live(&["writer"]), &new).expect("no refusal"),
            vec![renamed]
        );
        for tables in [&["writer", "author"][..], &["author"], &[]] {
            assert_eq!(
                live_table_hints(&live(tables), &new).expect("no refusal"),
                Vec::<Hint>::new(),
                "{tables:?}"
            );
        }
    }

    #[test]
    fn the_read_side_refuses_what_the_planner_refuses_with_its_text() {
        let twice = declared(vec![
            model("author", Some("writer")),
            model("poet", Some("writer")),
        ]);
        let refusal = live_table_hints(&live(&["writer"]), &twice).expect_err("refused");
        assert_eq!(Err(refusal.clone()), live_hints(&twice, &twice).map(|_| ()));
        assert_eq!(
            hint_refusal_warning(&refusal),
            crate::Report {
                kind: crate::ReportKind::HintRefused(refusal.clone()),
                subject: crate::Subject::Modelset,
                text: "rename hint refused: tables \"author\" and \"poet\" all declare \
                       __ferro_renamed_from__ = \"writer\": one table becomes one table; keep \
                       the hint on the model \"writer\" became"
                    .into(),
                recurs: true,
            }
        );
    }

    #[test]
    fn the_pending_warning_names_the_tables_waiting_and_both_doors() {
        assert_eq!(
            pending_table_rename_warning("writer", "author", &["book".into()]),
            crate::Report {
                kind: crate::ReportKind::PendingTableRename,
                subject: crate::Subject::table("author"),
                text: "table \"author\" declares __ferro_renamed_from__ = \"writer\", and the \
                       database holds \"writer\" and no \"author\": \"author\" was not created, \
                       nor \"book\", which reference it. The rename runs under connect(..., \
                       migrate_updates=True) or in a migration from ferro migrate new."
                    .into(),
                recurs: true,
            }
        );
    }
}

#[cfg(test)]
mod foreign_default_tests {
    //! Foreign defaults (ADR-0027, ADR-0034 as amended): a live server
    //! default ferro did not leave is a `DropDefault`, whose verdict is
    //! decided here once for every door.
    use super::*;
    use ferro_schema_ir::SchemaColumn;

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
            retired: false,
            generated: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: false,
            enum_renamed_labels: Default::default(),
            enum_retired_labels: Default::default(),
        }
    }

    /// A live column holding the server default `text`, as introspection
    /// reads it.
    fn with_default(col: SchemaColumn, text: &str) -> SchemaColumn {
        SchemaColumn {
            default: Some(serde_json::json!(text)),
            ..col
        }
    }

    fn author(columns: Vec<SchemaColumn>) -> SchemaModel {
        SchemaModel {
            renamed_from: None,
            model_name: "app.Author".into(),
            table_name: "author".into(),
            columns,
            foreign_keys: Vec::new(),
            indexes: Vec::new(),
            uniques: Vec::new(),
            checks: Vec::new(),
            table_checks: Vec::new(),
            row_security: None,
        }
    }

    fn live(columns: Vec<SchemaColumn>) -> Side {
        let mut facts = LiveFacts::default();
        facts
            .tables
            .insert("author".into(), LiveTableFacts::default());
        Side::live(envelope(vec![author(columns)]), facts).expect("live side")
    }

    fn declared(columns: Vec<SchemaColumn>) -> Side {
        Side::declared(envelope(vec![author(columns)]))
    }

    const DESTRUCTIVE: PlanOptions = PlanOptions { destructive: true };
    const UPDATES: PlanOptions = PlanOptions { destructive: false };

    fn ops(plan: &Plan) -> Vec<MigrationOp> {
        plan.ops().cloned().collect()
    }

    fn drop_default(column: &str, was: &str) -> MigrationOp {
        MigrationOp::DropDefault {
            table: "author".into(),
            column: column.into(),
            was: was.into(),
        }
    }

    /// The models: `id` and a required `slug`, no server default (ferro
    /// persists none).
    fn models() -> Side {
        declared(vec![
            SchemaColumn {
                autoincrement: true,
                ..column("id", "int", false)
            },
            SchemaColumn {
                default: Some(serde_json::json!("python-side")),
                ..column("slug", "varchar", false)
            },
        ])
    }

    fn serial_id(sequence: &str) -> SchemaColumn {
        SchemaColumn {
            autoincrement: true,
            ..with_default(
                column("id", "int", false),
                &format!("nextval('{sequence}'::regclass)"),
            )
        }
    }

    #[test]
    fn a_hand_set_postgres_default_is_dropped_natively_under_destructive() {
        let db = live(vec![
            serial_id("author_id_seq"),
            with_default(column("slug", "varchar", false), "''::character varying"),
        ]);
        let plan = plan_from_ir(&db, &models(), Dialect::Postgres, DESTRUCTIVE);
        assert_eq!(ops(&plan), vec![drop_default("slug", "''")]);
        let verdict = &plan.operations[0].verdict;
        assert_eq!(verdict.execution, Execution::Native);
        assert!(!verdict.drops_data && !verdict.demands_values && !verdict.recreates);
        assert_eq!(verdict.fails_on_rows, RowRisk::None);
        let rendered = plan.render().expect("renders");
        assert_eq!(
            rendered[0].statements,
            ["ALTER TABLE \"author\" ALTER COLUMN \"slug\" DROP DEFAULT"]
        );
        assert!(plan.reports.is_empty(), "{:?}", plan.reports);
    }

    #[test]
    fn without_destructive_the_foreign_default_stays_and_is_reported_with_its_fix() {
        let db = live(vec![
            serial_id("author_id_seq"),
            with_default(column("slug", "varchar", false), "''::character varying"),
        ]);
        let plan = plan_from_ir(&db, &models(), Dialect::Postgres, UPDATES);
        assert!(ops(&plan).is_empty());
        let [report] = plan.reports.as_slice() else {
            panic!("{:?}", plan.reports);
        };
        assert_eq!(
            report.kind,
            crate::ReportKind::ForeignDefault { was: "''".into() }
        );
        assert_eq!(report.subject, crate::Subject::column("author", "slug"));
        assert!(report.recurs);
        assert_eq!(
            report.text,
            "author.slug has default '' in the database, none declared; it is left in \
             place: connect(..., migrate_destructive=True) drops it, or run ALTER TABLE \
             \"author\" ALTER COLUMN \"slug\" DROP DEFAULT."
        );
    }

    #[test]
    fn a_serial_keys_sequence_is_ferros_own_whatever_it_is_called() {
        for sequence in ["author_id_seq", "writer_id_seq", "anything"] {
            let db = live(vec![serial_id(sequence), column("slug", "varchar", false)]);
            let plan = plan_from_ir(&db, &models(), Dialect::Postgres, DESTRUCTIVE);
            assert!(ops(&plan).is_empty(), "{sequence}: {:?}", ops(&plan));
        }
        // The same `nextval` text on a column that owns no sequence is not
        // recognised by its text.
        let db = live(vec![
            with_default(
                column("id", "int", false),
                "nextval('author_id_seq'::regclass)",
            ),
            column("slug", "varchar", false),
        ]);
        let plan = plan_from_ir(&db, &models(), Dialect::Postgres, DESTRUCTIVE);
        assert_eq!(
            ops(&plan),
            vec![drop_default("id", "nextval('author_id_seq'::regclass)")]
        );
    }

    #[test]
    fn sqlites_kept_literal_on_a_not_null_column_is_never_drift_whatever_the_literal() {
        for literal in ["'free'", "'x'", "0", "-1", "1.5", "TRUE", "NULL", "X'00'"] {
            let db = live(vec![
                column("id", "int", false),
                with_default(column("slug", "varchar", false), literal),
            ]);
            let plan = plan_from_ir(&db, &models(), Dialect::Sqlite, DESTRUCTIVE);
            assert!(ops(&plan).is_empty(), "{literal}: {:?}", ops(&plan));
        }
    }

    #[test]
    fn sqlite_an_expression_or_a_nullable_columns_default_is_a_rebuild() {
        let cases = [
            (column("slug", "varchar", false), "datetime('now')"),
            (column("slug", "varchar", false), "CURRENT_TIMESTAMP"),
            (column("slug", "varchar", true), "''"),
        ];
        for (col, text) in cases {
            let nullable = col.nullable;
            let db = live(vec![column("id", "int", false), with_default(col, text)]);
            let target = declared(vec![
                column("id", "int", false),
                column("slug", "varchar", nullable),
            ]);
            let plan = plan_from_ir(&db, &target, Dialect::Sqlite, DESTRUCTIVE);
            assert_eq!(ops(&plan), vec![drop_default("slug", text)], "{text}");
            let verdict = &plan.operations[0].verdict;
            assert_eq!(verdict.execution, Execution::Rebuild);
            assert!(!verdict.drops_data);
            let rendered = plan.render().expect("renders");
            assert!(rendered[0].statements.is_empty());
            let [report] = rendered[0].reports.as_slice() else {
                panic!("{:?}", rendered[0].reports);
            };
            assert_eq!(
                report.kind,
                crate::ReportKind::SqliteInPlace {
                    what: crate::InPlaceChange::DropDefault
                }
            );
            assert!(report.text.contains("ferro migrate new"), "{}", report.text);
            assert!(report.blocks());
        }
    }

    #[test]
    fn two_declared_snapshots_never_plan_a_drop_default() {
        let before = declared(vec![
            column("id", "int", false),
            with_default(column("slug", "varchar", false), "datetime('now')"),
        ]);
        for dialect in [Dialect::Postgres, Dialect::Sqlite] {
            let plan = plan_from_ir(&before, &models(), dialect, DESTRUCTIVE);
            assert!(
                !ops(&plan)
                    .iter()
                    .any(|op| matches!(op, MigrationOp::DropDefault { .. })),
                "{dialect:?}: {:?}",
                ops(&plan)
            );
        }
    }

    #[test]
    fn the_down_toward_the_database_is_irreversible_with_its_reason() {
        for (dialect, text, was) in [
            (Dialect::Postgres, "''::character varying", "''"),
            (Dialect::Sqlite, "datetime('now')", "datetime('now')"),
        ] {
            let db = live(vec![
                column("id", "int", false),
                with_default(column("slug", "varchar", false), text),
            ]);
            let up = plan_from_ir(&db, &models(), dialect, DESTRUCTIVE);
            let up_ops = ops(&up);
            assert_eq!(up_ops, vec![drop_default("slug", was)]);
            let down = plan_down(&up_ops, &models(), &db, dialect);
            let [PlannedOp { op, verdict }] = down.operations.as_slice() else {
                panic!("{dialect:?}: {:?}", down.operations);
            };
            assert_eq!(op, &drop_default("slug", was));
            assert!(!verdict.drops_data);
            let Execution::Irreversible(reason) = &verdict.execution else {
                panic!("{verdict:?}");
            };
            assert_eq!(
                reason,
                &format!(
                    "the default {was} on author.slug was set outside ferro, and no declared \
                     side holds it to put back; set it again by hand (ALTER TABLE \"author\" \
                     ALTER COLUMN \"slug\" SET DEFAULT {was}) if it is wanted"
                )
            );
        }
    }

    #[test]
    fn a_live_default_is_never_a_value_a_down_writes() {
        // The up drops a required column that holds a server default; its
        // down puts the column back with no value for the rows, as before
        // the live read carried defaults: the catalog text is no literal.
        let db = live(vec![
            column("id", "int", false),
            with_default(
                column("tier", "varchar", false),
                "'free'::character varying",
            ),
        ]);
        let models = declared(vec![column("id", "int", false)]);
        let up = plan_from_ir(&db, &models, Dialect::Postgres, DESTRUCTIVE);
        let up_ops = ops(&up);
        assert!(
            up_ops
                .iter()
                .all(|op| !matches!(op, MigrationOp::DropDefault { .. }))
        );
        let down = plan_down(&up_ops, &models, &db, Dialect::Postgres);
        let add = down
            .operations
            .iter()
            .find(|planned| matches!(planned.op, MigrationOp::AddColumn { .. }))
            .expect("the column comes back");
        assert!(add.verdict.demands_values, "{:?}", add.verdict);
    }

    #[test]
    fn a_literals_display_cast_is_folded_and_anything_else_kept() {
        assert_eq!(folded_default("''::character varying"), "''");
        assert_eq!(folded_default("'it''s'::text"), "'it''s'");
        assert_eq!(folded_default("'-1'::integer"), "'-1'");
        assert_eq!(folded_default("0"), "0");
        assert_eq!(
            folded_default("nextval('x_seq'::regclass)"),
            "nextval('x_seq'::regclass)"
        );
        assert_eq!(
            folded_default("('a'::text || 'b'::text)"),
            "('a'::text || 'b'::text)"
        );
        assert_eq!(folded_default("'a'::text || 'b'"), "'a'::text || 'b'");
    }

    #[test]
    fn sqlite_literal_shapes() {
        for literal in [
            "'x'", "''", "'it''s'", "0", "+1", "-2.5", "1e3", ".5", "0x1F", "x'00'",
        ] {
            assert!(is_sqlite_literal(literal), "{literal}");
        }
        for expression in [
            "datetime('now')",
            "CURRENT_TIMESTAMP",
            "'a' || 'b'",
            "inf",
            "nan",
            "1 + 1",
            "abs(-1)",
        ] {
            assert!(!is_sqlite_literal(expression), "{expression}");
        }
    }
}
