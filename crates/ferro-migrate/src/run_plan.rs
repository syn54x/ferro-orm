//! The run planner (ADR-0028): the migrations directory and the applied step
//! records in, the ordered pending steps or a refusal out. Every decision a
//! run makes before it touches the database lives here, as plain data with no
//! I/O, so `cargo test` pins each one:
//!
//! ```text
//! migrations/0001_create_author   records: 01 finished       -> applied
//! migrations/0002_add_teams       records: 01 started        -> resume at 01
//! migrations/0003_add_orgs        records: none              -> pending
//! ```
//!
//! [`plan_run`] returns the steps to run, each with the record it writes and
//! the [`ExecMode`] its headers ask for — going up, every pending step in
//! order; going down ([`Direction::Down`], ADR-0033), every recorded step
//! above the [`Target`] in reverse order, each with its down file; [`run_status`] answers `ferro migrate
//! status` from the same inputs; [`check_adoption`] and [`check_format`] are
//! the two refusals that need a fact read from the database (its live tables,
//! its tracking table's format number). [`split_statements`] cuts a step file
//! into the statements the executor sends one at a time, and
//! [`run_lock_key`] is the one derivation of the Postgres run-lock key.

use crate::Dialect;
use crate::directory::{
    DirectoryError, Headers, Migration, MigrationsDir, SNAPSHOT_FILE, Step, StepDialect, StepFile,
    StepKind,
};
use crate::snapshot::{Snapshot, encode_checksum, sha384};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

/// The tracking table format this ferro reads and writes.
pub const TRACKING_FORMAT: i64 = 1;

/// What a step record's attempt was (#466: the edit rules depend on it).
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum RecordKind {
    /// A transactional SQL step.
    #[serde(rename = "ddl")]
    Ddl,
    /// A `-- ferro: no-transaction` SQL step.
    #[serde(rename = "ddl-no-transaction")]
    DdlNoTransaction,
    /// An atomic data step.
    #[serde(rename = "atomic")]
    Atomic,
    /// A chunked data step.
    #[serde(rename = "chunked")]
    Chunked,
}

impl RecordKind {
    /// The spelling stored in the `kind` column.
    pub fn as_str(self) -> &'static str {
        match self {
            RecordKind::Ddl => "ddl",
            RecordKind::DdlNoTransaction => "ddl-no-transaction",
            RecordKind::Atomic => "atomic",
            RecordKind::Chunked => "chunked",
        }
    }

    /// The kind the `kind` column spells.
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "ddl" => Some(RecordKind::Ddl),
            "ddl-no-transaction" => Some(RecordKind::DdlNoTransaction),
            "atomic" => Some(RecordKind::Atomic),
            "chunked" => Some(RecordKind::Chunked),
            _ => None,
        }
    }
}

/// How a record came to be (#467's amendment to #466).
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Origin {
    /// The step ran.
    Run,
    /// `ferro migrate baseline` recorded it without running it.
    Baseline,
}

impl Origin {
    /// The spelling stored in the `origin` column.
    pub fn as_str(self) -> &'static str {
        match self {
            Origin::Run => "run",
            Origin::Baseline => "baseline",
        }
    }

    /// The origin the `origin` column spells.
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "run" => Some(Origin::Run),
            "baseline" => Some(Origin::Baseline),
            _ => None,
        }
    }
}

/// One row of `_ferro_migrations`: where one step of one migration stands on
/// this database. Timestamps are ISO-8601 UTC text (`2026-10-01T14:02:33.000000Z`).
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct StepRecord {
    /// `NNNN`.
    pub migration: u16,
    /// `NN`.
    pub step: u8,
    /// `NNNN_<name>`.
    pub migration_name: String,
    /// The file executed (`01_schema.up.postgres.sql`).
    pub file: String,
    /// What the attempt was.
    pub kind: RecordKind,
    /// SHA-384 of the executed bytes, lowercase hex.
    pub checksum: String,
    /// SHA-384 of the migration's `ir.json`, lowercase hex.
    pub snapshot_checksum: String,
    /// When the step first started.
    #[serde(default)]
    pub started_at: String,
    /// When it finished; `None` = not applied.
    #[serde(default)]
    pub finished_at: Option<String>,
    /// When the last attempt failed (diagnostics only).
    #[serde(default)]
    pub failed_at: Option<String>,
    /// The last attempt's error (diagnostics only).
    #[serde(default)]
    pub error: Option<String>,
    /// A chunked step's order-key cursor, as JSON.
    #[serde(default)]
    pub resume_cursor: Option<String>,
    /// A chunked step's committed rows.
    #[serde(default)]
    pub rows_done: Option<i64>,
    /// Milliseconds spent, summed across resumed runs.
    #[serde(default)]
    pub duration_ms: i64,
    /// The ferro that last ran the step.
    #[serde(default)]
    pub ferro_version: String,
    /// Ran, or recorded by a baseline.
    pub origin: Origin,
    /// A chunked `down` is part-way (declared by #520; #532 drives it).
    #[serde(default)]
    pub reverting: bool,
    /// A chunked `down`'s own cursor (declared by #520; #532 drives it).
    #[serde(default)]
    pub revert_cursor: Option<String>,
}

impl StepRecord {
    /// Whether the step finished.
    pub fn is_finished(&self) -> bool {
        self.finished_at.is_some()
    }

    /// `NNNN_<name>/<file>`.
    pub fn path(&self) -> String {
        format!("{}/{}", self.migration_name, self.file)
    }
}

/// Where `ferro migrate down` stops (#473: `down`, `--to 0005`,
/// `--to 0007:02`, `--all`). As JSON: `"latest"`, `{"migration": 5}`,
/// `{"step": [7, 2]}`, `"all"`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Target {
    /// Keep this migration fully applied; revert everything above it
    /// (`--to 0005`; `--to 0000` reverts everything).
    Migration(u16),
    /// Keep steps up to this one of this migration; revert everything above
    /// it (`--to 0007:02`).
    Step(u16, u8),
    /// Revert everything (`--all`).
    All,
    /// Revert the latest migration with a record, a partly applied one
    /// included (`down` with no target).
    Latest,
}

/// Which way a run goes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(tag = "direction", rename_all = "lowercase")]
pub enum Direction {
    /// Apply every pending step.
    Up,
    /// Revert every recorded step above `target`, in reverse order.
    Down {
        /// Where to stop.
        target: Target,
    },
}

/// How the executor runs a SQL step's statements (ADR-0024, ADR-0034).
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecMode {
    /// One transaction: the statements and the finished mark commit together.
    Transactional,
    /// `-- ferro: no-transaction` (Postgres): each statement on its own, in
    /// autocommit, re-runnable from the first.
    NoTransaction,
    /// `-- ferro: foreign-keys-off` (SQLite): one dedicated connection with
    /// the pragma off, `BEGIN IMMEDIATE`, `foreign_key_check`, the record,
    /// `COMMIT`, the pragma restored.
    ForeignKeysOff,
}

impl ExecMode {
    /// The record kind a SQL step of this mode writes.
    pub fn record_kind(self) -> RecordKind {
        match self {
            ExecMode::NoTransaction => RecordKind::DdlNoTransaction,
            ExecMode::Transactional | ExecMode::ForeignKeysOff => RecordKind::Ddl,
        }
    }
}

/// A started-but-unfinished step whose file changed since that attempt:
/// accepted and re-recorded (ADR-0030); the run's output says so.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct EditedUnfinished {
    /// The checksum the unfinished attempt recorded.
    pub recorded: String,
    /// The file the unfinished attempt recorded.
    pub recorded_file: String,
}

/// One step the run executes.
#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct PlannedStep {
    /// `NNNN`.
    pub migration: u16,
    /// `NNNN_<name>`.
    pub migration_name: String,
    /// `NN`.
    pub step: u8,
    /// The file the run executes: the up file going up
    /// (`01_schema.up.sqlite.sql`), the down file going down.
    pub file: String,
    /// That file's path.
    pub path: PathBuf,
    /// The record kind this attempt writes (going down: the standing record's).
    pub kind: RecordKind,
    /// SHA-384 of that file's raw bytes, lowercase hex.
    pub checksum: String,
    /// SHA-384 of the migration's `ir.json`, lowercase hex.
    pub snapshot_checksum: String,
    /// That file's headers (informational once planned: `mode` decides).
    #[serde(skip_deserializing)]
    pub headers: Headers,
    /// How the executor runs it.
    pub mode: ExecMode,
    /// Whether an unfinished record exists (the run resumes at this step).
    pub resumes: bool,
    /// Set when the unfinished attempt ran a different file.
    pub edited: Option<EditedUnfinished>,
    /// A Python data step (`NN_<name>.py`, ADR-0024): Python loads the file,
    /// checks it against `checksum`, reads its declared shape, runs it and
    /// writes its record with that shape as the kind; `kind` and `mode` are
    /// the atomic step's until the declaration says otherwise. Going down
    /// `file` is the same file, whose `down` the run calls.
    #[serde(default)]
    pub data: bool,
    /// Going down: why the step's down runs no statement — the down file's
    /// `-- ferro: nothing-to-reverse <reason>`, or an up that never finished
    /// in a transaction and so left nothing behind. Its record is removed.
    #[serde(default)]
    pub nothing_to_reverse: Option<String>,
    /// Going up, the record this step writes (timestamps and `ferro_version`
    /// filled at execution); going down, the standing record its down
    /// removes.
    pub record: StepRecord,
}

/// What a run executes, in order.
#[derive(Clone, Debug, Default, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct RunPlan {
    /// The pending steps, by migration then ordinal.
    pub steps: Vec<PlannedStep>,
    /// Migrations this database has applied that the directory lacks, when
    /// `allow_ahead` let the run through (`NNNN_<name>`).
    pub ahead: Vec<String>,
}

/// Why a run does not start. `Display` renders the text the operator reads,
/// naming the fix and ending in what was applied (nothing).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RunRefusal {
    /// A finished step's file changed (#466 "edited applied file").
    EditedApplied {
        /// `NNNN_<name>`.
        migration_name: String,
        /// `NNNN`.
        migration: u16,
        /// The file the record names.
        file: String,
        /// The recorded checksum.
        applied: String,
        /// When it finished (ISO-8601).
        applied_at: String,
        /// The checksum of the file on disk (`None` when the file was renamed
        /// away or deleted).
        on_disk: Option<String>,
    },
    /// A record's snapshot checksum is not the on-disk `ir.json`'s.
    SnapshotMismatch {
        /// `NNNN_<name>`.
        migration_name: String,
        /// The recorded snapshot checksum.
        applied: String,
        /// The on-disk snapshot's checksum.
        on_disk: String,
    },
    /// An `ir.json`'s `parent_checksum` is not its predecessor's checksum.
    BrokenChain {
        /// `NNNN_<name>` of the migration whose link is wrong.
        child: String,
        /// `NNNN_<name>` of its predecessor (`None` when the child is `0001`).
        parent: Option<String>,
        /// The parent checksum the child records.
        expected: Option<String>,
        /// The predecessor's actual checksum.
        actual: Option<String>,
    },
    /// A pending migration sorts below an applied one.
    OutOfOrder {
        /// `NNNN_<name>` of the pending migration.
        pending: String,
        /// `NNNN_<name>` of the applied one above it.
        applied: String,
        /// The number regenerating it at the head would take.
        next: u16,
    },
    /// The database holds records for migrations the directory lacks.
    AppliedMissing {
        /// What is applied but absent (`NNNN_<name>` or `NNNN_<name>/<file>`).
        missing: Vec<String>,
        /// The directory's name, as the operator knows it (`migrations`).
        directory: String,
    },
    /// The tracking table's format is newer than this ferro reads.
    NewerFormat {
        /// The format the database records.
        found: i64,
        /// This ferro's version.
        this_version: String,
        /// The ferro that last migrated the database, when a record says.
        last_version: Option<String>,
    },
    /// No records, but a table of the first migration's snapshot exists.
    TablesExist {
        /// `NNNN` of the first pending migration.
        migration: u16,
        /// The live tables it would create.
        tables: Vec<String>,
    },
    /// A `down` stopped part-way and left a record `reverting`.
    Reverting {
        /// `NNNN_<name>/<file>`.
        step: String,
    },
    /// A step `down` would have to revert is declared irreversible
    /// (`-- ferro: irreversible <reason>`, ADR-0033). Nothing is reverted,
    /// including the steps above it; there is no flag to skip it.
    Irreversible {
        /// `NNNN`.
        migration: u16,
        /// `NN`.
        step: u8,
        /// The declared reason.
        reason: String,
    },
    /// `down` would revert a migration `baseline` recorded (ADR-0031): its
    /// down would drop tables it never created.
    BelowBaseline {
        /// `NNNN` of the highest baselined migration: the floor.
        migration: u16,
    },
    /// `--to` names a migration or step the directory does not hold.
    NoSuchTarget {
        /// The target as the operator wrote it (`0009`, `0003:04`).
        target: String,
        /// The directory's name (`migrations`).
        directory: String,
    },
    /// A direction this ferro does not run yet.
    NotImplemented {
        /// What is not implemented.
        what: String,
    },
    /// A DDL step with no rendering for this database's dialect (#473).
    MissingRendering {
        /// `NNNN`.
        migration: u16,
        /// `NN`.
        step: u8,
        /// `NN_<name>`.
        step_name: String,
        /// This database's dialect.
        dialect: &'static str,
        /// The dialects the step has renderings for.
        rendered_for: Vec<&'static str>,
    },
    /// A header this dialect cannot honour, or two that contradict.
    BadHeaders {
        /// `NNNN_<name>/<file>`.
        file: String,
        /// What is wrong and how to fix it.
        reason: String,
    },
    /// The migrations directory itself is malformed.
    Directory {
        /// The reader's refusal.
        error: DirectoryError,
    },
}

impl RunRefusal {
    /// A stable identifier for the refusal, used by reports and tests.
    pub fn kind(&self) -> &'static str {
        match self {
            RunRefusal::EditedApplied { .. } => "edited_applied",
            RunRefusal::SnapshotMismatch { .. } => "snapshot_mismatch",
            RunRefusal::BrokenChain { .. } => "broken_chain",
            RunRefusal::OutOfOrder { .. } => "out_of_order",
            RunRefusal::AppliedMissing { .. } => "applied_missing",
            RunRefusal::NewerFormat { .. } => "newer_format",
            RunRefusal::TablesExist { .. } => "tables_exist",
            RunRefusal::Reverting { .. } => "reverting",
            RunRefusal::Irreversible { .. } => "irreversible",
            RunRefusal::BelowBaseline { .. } => "below_baseline",
            RunRefusal::NoSuchTarget { .. } => "no_such_target",
            RunRefusal::NotImplemented { .. } => "not_implemented",
            RunRefusal::MissingRendering { .. } => "missing_rendering",
            RunRefusal::BadHeaders { .. } => "bad_headers",
            RunRefusal::Directory { .. } => "directory",
        }
    }

    /// Whether the refusal is about the database's state against the
    /// directory (what `status` reports as needing attention), rather than a
    /// capability this ferro lacks.
    pub fn needs_attention(&self) -> bool {
        !matches!(self, RunRefusal::NotImplemented { .. })
    }
}

/// `2026-10-01T14:02:33.123Z` → `2026-10-01 14:02 UTC`.
fn short_time(iso: &str) -> String {
    match (iso.get(0..10), iso.get(11..16)) {
        (Some(date), Some(time)) => format!("{date} {time} UTC"),
        _ => iso.to_string(),
    }
}

fn and_list(items: &[String]) -> String {
    match items {
        [] => String::new(),
        [one] => one.clone(),
        [init @ .., last] => format!("{} and {last}", init.join(", ")),
    }
}

impl std::fmt::Display for RunRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RunRefusal::EditedApplied {
                migration_name,
                migration,
                file,
                applied,
                applied_at,
                on_disk,
            } => {
                let on_disk = match on_disk {
                    Some(checksum) => format!("sha384:{checksum}"),
                    None => "missing".to_string(),
                };
                write!(
                    f,
                    "ferro migrate: {migration_name}/{file} was edited after it was applied to \
                     this database.\n  applied   sha384:{applied}  ({})\n  on disk   {on_disk}\n\
                     An applied step is never run again. Restore the file, or accept a \
                     deliberate edit with\n`ferro migrate rerecord {migration:04}`. Nothing was \
                     applied.",
                    short_time(applied_at)
                )
            }
            RunRefusal::SnapshotMismatch {
                migration_name,
                applied,
                on_disk,
            } => write!(
                f,
                "ferro migrate: {migration_name}/{SNAPSHOT_FILE} is not the snapshot this \
                 database applied {migration_name} under.\n  applied   sha384:{applied}\n  \
                 on disk   sha384:{on_disk}\nA snapshot is never edited: later migrations and \
                 historical models are built from it.\nRestore the file. Nothing was applied."
            ),
            RunRefusal::BrokenChain {
                child,
                parent: Some(parent),
                expected,
                actual,
            } => {
                let left = format!("{child}/{SNAPSHOT_FILE} expects parent");
                let right = format!("{parent}/{SNAPSHOT_FILE} is");
                let width = left.len().max(right.len()) + 3;
                let show = |c: &Option<String>| match c {
                    Some(c) => format!("sha384:{c}"),
                    None => "null".to_string(),
                };
                write!(
                    f,
                    "ferro migrate: the migration chain is broken at {child}.\n  \
                     {left:<width$}{}\n  {right:<width$}{}\n{parent}'s snapshot changed after \
                     {child} was generated. Restore it, or\nregenerate {child}. Nothing was \
                     applied.",
                    show(expected),
                    show(actual)
                )
            }
            RunRefusal::BrokenChain {
                child,
                parent: None,
                expected,
                ..
            } => write!(
                f,
                "ferro migrate: the migration chain is broken at {child}.\n  \
                 {child}/{SNAPSHOT_FILE} expects parent   sha384:{}\n{child} is the first \
                 migration, so its parent_checksum must be null. Restore it. Nothing was \
                 applied.",
                expected.as_deref().unwrap_or("")
            ),
            RunRefusal::OutOfOrder {
                pending,
                applied,
                next,
            } => write!(
                f,
                "ferro migrate: {pending} is pending, but {applied} is already applied to this \
                 database.\nMigrations apply in order, with no override. Regenerate {pending} \
                 at the head\n(it becomes {next:04}). Nothing was applied."
            ),
            RunRefusal::AppliedMissing { missing, directory } => {
                let verb = if missing.len() == 1 {
                    "which is"
                } else {
                    "which are"
                };
                write!(
                    f,
                    "ferro migrate: this database has applied {}, {verb} not in {directory}/.\n\
                     The directory is behind the database: check out the branch that holds it. \
                     Nothing was applied.",
                    and_list(missing)
                )
            }
            RunRefusal::NewerFormat {
                found,
                this_version,
                last_version,
            } => {
                let last = match last_version {
                    Some(version) => format!("It was last migrated by ferro {version}."),
                    None => "It was set up by a newer ferro.".to_string(),
                };
                write!(
                    f,
                    "ferro migrate: this database's tracking table is format {found}; this ferro \
                     ({this_version}) understands format {TRACKING_FORMAT}.\n{last} Upgrade ferro \
                     to run migrations against it."
                )
            }
            RunRefusal::TablesExist { migration, tables } => {
                let quoted: Vec<String> = tables.iter().map(|t| format!("\"{t}\"")).collect();
                let (noun, verb) = if tables.len() == 1 {
                    ("table", "exists")
                } else {
                    ("tables", "exist")
                };
                write!(
                    f,
                    "This database has no ferro migration records, but {noun} {} from \
                     {migration:04} already {verb}. If it was built by auto-migrate or Alembic, \
                     run \"ferro migrate baseline {migration:04}\". up will not run \
                     {migration:04} over it.",
                    and_list(&quoted)
                )
            }
            RunRefusal::Reverting { step } => write!(
                f,
                "ferro migrate: {step} is part-way through being reverted (its record is \
                 reverting).\nFinish the revert with `ferro migrate down` before running up. \
                 Nothing was applied."
            ),
            RunRefusal::Irreversible {
                migration,
                step,
                reason,
            } => write!(
                f,
                "ferro migrate: {migration:04}:{step:02} is irreversible: {reason}\nThere is no \
                 flag to skip it: to revert past it, write the step's down in place of the \
                 declaration. Nothing was reverted."
            ),
            RunRefusal::BelowBaseline { migration } => write!(
                f,
                "ferro migrate: {migration:04} was recorded by `baseline` and created nothing \
                 here; `down` can go no lower than {migration:04}. Nothing was reverted."
            ),
            RunRefusal::NoSuchTarget { target, directory } => write!(
                f,
                "ferro migrate: --to {target} names nothing in {directory}/: give a migration \
                 number (0005), a migration and step (0005:02), or 0000 for everything. \
                 Nothing was reverted."
            ),
            RunRefusal::NotImplemented { what } => write!(f, "not implemented yet: {what}"),
            RunRefusal::MissingRendering {
                migration,
                step,
                step_name,
                dialect,
                rendered_for,
            } => write!(
                f,
                "{migration:04} step {step:02} has no {dialect} rendering. This database is \
                 {dialect}; the migration was generated for: {}. Add \"{dialect}\" to dialects \
                 and regenerate, or write {step_name}.up.{dialect}.sql.",
                rendered_for.join(", ")
            ),
            RunRefusal::BadHeaders { file, reason } => {
                write!(f, "ferro migrate: {file}: {reason}. Nothing was applied.")
            }
            RunRefusal::Directory { error } => {
                write!(f, "ferro migrate: {error}. Nothing was applied.")
            }
        }
    }
}

impl std::error::Error for RunRefusal {}

/// Read the migrations directory for a run: [`MigrationsDir::read`], with a
/// broken snapshot chain reported as #466's refusal, both checksums shown.
///
/// # Errors
/// [`RunRefusal::BrokenChain`] or [`RunRefusal::Directory`].
pub fn read_for_run(path: &Path) -> Result<MigrationsDir, RunRefusal> {
    MigrationsDir::read(path).map_err(|error| match &error {
        DirectoryError::BrokenChain { parent, child } => {
            let migration_of = |file: &Path| {
                file.parent()
                    .map(|dir| dir.display().to_string())
                    .unwrap_or_default()
            };
            let expected = std::fs::read(path.join(child))
                .ok()
                .and_then(|bytes| Snapshot::load(&bytes).ok())
                .and_then(|snapshot| snapshot.parent_checksum)
                .map(|c| encode_checksum(&c));
            let actual = parent
                .as_ref()
                .and_then(|parent| std::fs::read(path.join(parent)).ok())
                .map(|bytes| encode_checksum(&sha384(&bytes)));
            RunRefusal::BrokenChain {
                child: migration_of(child),
                parent: parent.as_deref().map(migration_of),
                expected,
                actual,
            }
        }
        _ => RunRefusal::Directory { error },
    })
}

fn dialect_name(dialect: Dialect) -> &'static str {
    match dialect {
        Dialect::Postgres => "postgres",
        Dialect::Sqlite => "sqlite",
    }
}

/// A step file's name, as a record stores it (`01_schema.up.sqlite.sql`).
pub fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default()
}

/// The file of `step` this database executes: its dialect's rendering, else
/// the portable file. Every record names this file: what `up` runs and
/// checks applied records against, and what `baseline` records.
///
/// # Errors
/// [`RunRefusal::MissingRendering`] when the step has neither.
pub fn step_file<'a>(
    migration: &Migration,
    step: &'a Step,
    dialect: Dialect,
) -> Result<&'a StepFile, RunRefusal> {
    if let Some(file) = step
        .files
        .get(&StepDialect::from(dialect))
        .or_else(|| step.files.get(&StepDialect::Portable))
    {
        return Ok(file);
    }
    Err(RunRefusal::MissingRendering {
        migration: migration.number,
        step: step.ordinal,
        step_name: format!("{:02}_{}", step.ordinal, step.name),
        dialect: dialect_name(dialect),
        rendered_for: step.files.keys().filter_map(|d| d.suffix()).collect(),
    })
}

/// The [`ExecMode`] a SQL step's headers ask for on `dialect`.
///
/// # Errors
/// [`RunRefusal::BadHeaders`] when both modes are declared, when
/// `no-transaction` is declared on SQLite (ADR-0034: Postgres-only) or
/// `foreign-keys-off` on Postgres (SQLite-only).
pub fn exec_mode(headers: &Headers, dialect: Dialect, file: &str) -> Result<ExecMode, RunRefusal> {
    let bad = |reason: &str| RunRefusal::BadHeaders {
        file: file.to_string(),
        reason: reason.to_string(),
    };
    match (headers.no_transaction, headers.foreign_keys_off, dialect) {
        (true, true, _) => Err(bad(
            "declares both no-transaction and foreign-keys-off; a step runs one way, so keep one \
             header",
        )),
        (true, false, Dialect::Postgres) => Ok(ExecMode::NoTransaction),
        (true, false, Dialect::Sqlite) => Err(bad(
            "declares no-transaction, which is Postgres-only; SQLite runs every step in a \
             transaction, so remove the header from the sqlite rendering",
        )),
        (false, true, Dialect::Sqlite) => Ok(ExecMode::ForeignKeysOff),
        (false, true, Dialect::Postgres) => Err(bad(
            "declares foreign-keys-off, which is SQLite-only; remove the header from the \
             postgres rendering",
        )),
        (false, false, _) => Ok(ExecMode::Transactional),
    }
}

fn directory_label(dir: &MigrationsDir) -> String {
    dir.path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| dir.path.display().to_string())
}

type RecordMap<'a> = BTreeMap<(u16, u8), &'a StepRecord>;

/// The refusals that hold whichever way a run goes, from the records alone:
/// a reverting record, and records the directory has no migration or step
/// for. Returns the ahead migrations `allow_ahead` let through.
fn check_records(
    dir: &MigrationsDir,
    records: &[StepRecord],
    allow_ahead: bool,
    refuse_reverting: bool,
) -> Result<Vec<String>, RunRefusal> {
    if refuse_reverting && let Some(record) = records.iter().find(|r| r.reverting) {
        return Err(RunRefusal::Reverting {
            step: record.path(),
        });
    }
    let head = dir.head().map_or(0, |m| m.number);
    let mut ahead = BTreeSet::new();
    let mut missing = BTreeSet::new();
    for record in records {
        match dir
            .migrations
            .get(usize::from(record.migration).wrapping_sub(1))
        {
            None if record.migration > head => {
                ahead.insert(record.migration_name.clone());
            }
            None => {
                missing.insert(record.migration_name.clone());
            }
            Some(migration) if migration.dir_name() != record.migration_name => {
                missing.insert(record.migration_name.clone());
            }
            Some(migration) if !migration.steps.iter().any(|s| s.ordinal == record.step) => {
                missing.insert(record.path());
            }
            Some(_) => {}
        }
    }
    if !missing.is_empty() || (!ahead.is_empty() && !allow_ahead) {
        return Err(RunRefusal::AppliedMissing {
            missing: ahead.iter().chain(missing.iter()).cloned().collect(),
            directory: directory_label(dir),
        });
    }
    Ok(ahead.into_iter().collect())
}

/// Every applied step and snapshot against the directory: a snapshot whose
/// checksum is not the recorded one, a finished step whose file changed.
fn check_applied(
    dir: &MigrationsDir,
    by_key: &RecordMap,
    dialect: Dialect,
) -> Result<(), RunRefusal> {
    for migration in &dir.migrations {
        let snapshot = encode_checksum(&migration.snapshot.checksum);
        for step in &migration.steps {
            let Some(record) = by_key.get(&(migration.number, step.ordinal)) else {
                continue;
            };
            if record.snapshot_checksum != snapshot {
                return Err(RunRefusal::SnapshotMismatch {
                    migration_name: migration.dir_name(),
                    applied: record.snapshot_checksum.clone(),
                    on_disk: snapshot,
                });
            }
            if !record.is_finished() {
                continue;
            }
            let file = step_file(migration, step, dialect)?;
            let checksum = encode_checksum(&file.up_checksum);
            if record.checksum != checksum || record.file != file_name(&file.up) {
                return Err(RunRefusal::EditedApplied {
                    migration_name: migration.dir_name(),
                    migration: migration.number,
                    file: record.file.clone(),
                    applied: record.checksum.clone(),
                    applied_at: record.finished_at.clone().unwrap_or_default(),
                    on_disk: (record.file == file_name(&file.up)).then_some(checksum),
                });
            }
        }
    }
    Ok(())
}

/// Whether every step of `migration` has a finished record.
fn is_applied(migration: &Migration, by_key: &RecordMap) -> bool {
    migration.steps.iter().all(|s| {
        by_key
            .get(&(migration.number, s.ordinal))
            .is_some_and(|r| r.is_finished())
    })
}

/// A pending migration below one with records is out of order (ADR-0028).
fn check_order(dir: &MigrationsDir, by_key: &RecordMap) -> Result<(), RunRefusal> {
    let recorded: BTreeSet<u16> = by_key.keys().map(|(m, _)| *m).collect();
    for migration in &dir.migrations {
        if is_applied(migration, by_key) {
            continue;
        }
        if let Some(above) = recorded
            .iter()
            .filter(|n| **n > migration.number)
            .find_map(|n| dir.migrations.get(usize::from(*n) - 1))
        {
            return Err(RunRefusal::OutOfOrder {
                pending: migration.dir_name(),
                applied: above.dir_name(),
                next: dir.head().map_or(1, |head| head.number + 1),
            });
        }
    }
    Ok(())
}

/// Plan a run: every pending step of every pending migration, in order, or
/// the refusal that stops it before anything runs.
///
/// A step is pending when it has no finished record. A started-but-unfinished
/// record resumes at that step; if its file changed since, the edit is
/// accepted and re-recorded (ADR-0030), and [`PlannedStep::edited`] says so.
///
/// # Errors
/// A [`RunRefusal`]: a reverting record; records for migrations the directory
/// lacks (allowed through with `allow_ahead` when they all sort above its
/// head); a snapshot or finished step edited after it was applied; a pending
/// migration below an applied one; a DDL step without this dialect's
/// rendering; headers the dialect cannot honour. Going down, see
/// [`plan_down`]'s refusals.
pub fn plan_run(
    dir: &MigrationsDir,
    records: &[StepRecord],
    dialect: Dialect,
    direction: Direction,
    allow_ahead: bool,
) -> Result<RunPlan, RunRefusal> {
    if let Direction::Down { target } = direction {
        return plan_down(dir, records, dialect, target);
    }
    let ahead = check_records(dir, records, allow_ahead, true)?;
    let by_key: RecordMap = records.iter().map(|r| ((r.migration, r.step), r)).collect();
    check_applied(dir, &by_key, dialect)?;
    check_order(dir, &by_key)?;

    let mut steps = Vec::new();
    for migration in &dir.migrations {
        let snapshot_checksum = encode_checksum(&migration.snapshot.checksum);
        for step in &migration.steps {
            let record = by_key.get(&(migration.number, step.ordinal));
            if record.is_some_and(|r| r.is_finished()) {
                continue;
            }
            let file = step_file(migration, step, dialect)?;
            let name = file_name(&file.up);
            let shown = format!("{}/{name}", migration.dir_name());
            let mode = exec_mode(&file.headers, dialect, &shown)?;
            let data = step.kind == StepKind::Data;
            // A data step records the shape its file declares; Python sets it.
            let kind = if data {
                RecordKind::Atomic
            } else {
                mode.record_kind()
            };
            let checksum = encode_checksum(&file.up_checksum);
            let edited = record
                .filter(|r| r.checksum != checksum || r.file != name)
                .map(|r| EditedUnfinished {
                    recorded: r.checksum.clone(),
                    recorded_file: r.file.clone(),
                });
            steps.push(PlannedStep {
                migration: migration.number,
                migration_name: migration.dir_name(),
                step: step.ordinal,
                file: name.clone(),
                path: file.up.clone(),
                kind,
                checksum: checksum.clone(),
                snapshot_checksum: snapshot_checksum.clone(),
                headers: file.headers.clone(),
                mode,
                resumes: record.is_some(),
                edited,
                nothing_to_reverse: None,
                data,
                record: StepRecord {
                    migration: migration.number,
                    step: step.ordinal,
                    migration_name: migration.dir_name(),
                    file: name,
                    kind,
                    checksum,
                    snapshot_checksum: snapshot_checksum.clone(),
                    started_at: String::new(),
                    finished_at: None,
                    failed_at: None,
                    error: None,
                    resume_cursor: None,
                    rows_done: None,
                    duration_ms: 0,
                    ferro_version: String::new(),
                    origin: Origin::Run,
                    reverting: false,
                    revert_cursor: None,
                },
            });
        }
    }
    Ok(RunPlan { steps, ahead })
}

/// Why an unfinished transactional step's down runs nothing.
const UNFINISHED_TRANSACTIONAL: &str =
    "its up never finished, and a transactional step that does not finish leaves nothing behind";

/// The highest `(migration, step)` a `down` to `target` keeps; every record
/// above it is reverted.
fn down_floor(
    dir: &MigrationsDir,
    records: &[StepRecord],
    target: Target,
) -> Result<(u16, u8), RunRefusal> {
    let no_such = |target: String| RunRefusal::NoSuchTarget {
        target,
        directory: directory_label(dir),
    };
    match target {
        Target::All | Target::Migration(0) => Ok((0, 0)),
        Target::Latest => Ok(records
            .iter()
            .map(|r| r.migration)
            .max()
            .map_or((0, 0), |latest| (latest.saturating_sub(1), u8::MAX))),
        Target::Migration(number) => dir
            .migrations
            .get(usize::from(number) - 1)
            .map(|_| (number, u8::MAX))
            .ok_or_else(|| no_such(format!("{number:04}"))),
        Target::Step(number, step) => dir
            .migrations
            .get(usize::from(number).wrapping_sub(1))
            .filter(|m| m.steps.iter().any(|s| s.ordinal == step))
            .map(|_| (number, step))
            .ok_or_else(|| no_such(format!("{number:04}:{step:02}"))),
    }
}

/// Plan a `down` (ADR-0033): every recorded step above `target`, newest
/// first, each with its down file — or the refusal that stops the run before
/// it reverts anything.
///
/// A step's down runs in the [`ExecMode`] its down file's headers ask for. A
/// `-- ferro: nothing-to-reverse <reason>` down, and an unfinished
/// transactional step (whose attempt rolled back), run no statement: their
/// record is removed. An unfinished no-transaction step runs its down, since
/// the statements before its failure stayed applied.
///
/// # Errors
/// Before anything is reverted: a record for a migration or step the
/// directory lacks (the down files come only from disk, so a database ahead
/// of the checkout is refused whatever `allow_ahead` says); a snapshot or
/// finished step edited since it was applied; a `--to` naming nothing;
/// [`RunRefusal::BelowBaseline`] for a step a baseline recorded;
/// [`RunRefusal::Irreversible`] for a step whose down declares it so, quoting
/// the reason; down headers the dialect cannot honour. A data step's down is
/// its own file's `down` function: Python reads its declaration (ADR-0035)
/// and refuses an irreversible one before the lock.
fn plan_down(
    dir: &MigrationsDir,
    records: &[StepRecord],
    dialect: Dialect,
    target: Target,
) -> Result<RunPlan, RunRefusal> {
    check_records(dir, records, false, false)?;
    let by_key: RecordMap = records.iter().map(|r| ((r.migration, r.step), r)).collect();
    check_applied(dir, &by_key, dialect)?;
    let floor = down_floor(dir, records, target)?;
    let baseline_floor = records
        .iter()
        .filter(|r| r.origin == Origin::Baseline)
        .map(|r| r.migration)
        .max();

    let mut steps = Vec::new();
    for (&(number, ordinal), record) in by_key.iter().rev() {
        if (number, ordinal) <= floor {
            break;
        }
        if let Some(floor) = baseline_floor
            && number <= floor
        {
            return Err(RunRefusal::BelowBaseline { migration: floor });
        }
        // `check_records` refused every record the directory lacks.
        let Some((migration, step)) = dir
            .migrations
            .get(usize::from(number) - 1)
            .and_then(|m| Some((m, m.steps.iter().find(|s| s.ordinal == ordinal)?)))
        else {
            continue;
        };
        let file = step_file(migration, step, dialect)?;
        let data = step.kind == StepKind::Data;
        let (down, down_checksum) = match (&file.down, &file.down_checksum, data) {
            // A data step's down lives in the same file as its up.
            (_, _, true) => (&file.up, &file.up_checksum),
            (Some(down), Some(checksum), false) => (down, checksum),
            // The directory reader refuses an up without its down.
            _ => {
                return Err(RunRefusal::Directory {
                    error: DirectoryError::MissingPair {
                        present: file.up.clone(),
                        missing: file.up.with_extension("down.sql"),
                    },
                });
            }
        };
        if let Some(reason) = &file.down_headers.irreversible {
            return Err(RunRefusal::Irreversible {
                migration: number,
                step: ordinal,
                reason: reason.clone(),
            });
        }
        let name = file_name(down);
        let shown = format!("{}/{name}", migration.dir_name());
        let mode = exec_mode(&file.down_headers, dialect, &shown)?;
        let nothing_to_reverse = file.down_headers.nothing_to_reverse.clone().or_else(|| {
            (!record.is_finished() && matches!(record.kind, RecordKind::Ddl | RecordKind::Atomic))
                .then(|| UNFINISHED_TRANSACTIONAL.to_string())
        });
        steps.push(PlannedStep {
            migration: number,
            migration_name: migration.dir_name(),
            step: ordinal,
            file: name,
            path: down.clone(),
            kind: record.kind,
            checksum: encode_checksum(down_checksum),
            snapshot_checksum: record.snapshot_checksum.clone(),
            headers: file.down_headers.clone(),
            mode,
            resumes: false,
            edited: None,
            nothing_to_reverse,
            data,
            record: (*record).clone(),
        });
    }
    Ok(RunPlan {
        steps,
        ahead: Vec::new(),
    })
}

/// The refusal for a database with no records that already holds a table
/// the first migration creates (#473: it names `ferro migrate baseline`).
/// `live_tables` are the database's tables in the governed schema.
///
/// # Errors
/// [`RunRefusal::TablesExist`] naming the live tables of the first
/// migration's snapshot.
pub fn check_adoption(
    dir: &MigrationsDir,
    records: &[StepRecord],
    live_tables: &[String],
) -> Result<(), RunRefusal> {
    if !records.is_empty() {
        return Ok(());
    }
    let Some(first) = dir.migrations.first() else {
        return Ok(());
    };
    let live: BTreeSet<&str> = live_tables.iter().map(String::as_str).collect();
    let tables: Vec<String> = first
        .snapshot
        .ir
        .payload
        .models
        .iter()
        .map(|model| model.table_name.clone())
        .filter(|table| live.contains(table.as_str()))
        .collect();
    if tables.is_empty() {
        return Ok(());
    }
    Err(RunRefusal::TablesExist {
        migration: first.number,
        tables,
    })
}

/// The tracking table's format against this ferro's (#466): a newer one is
/// refused, naming the ferro that last migrated the database.
///
/// # Errors
/// [`RunRefusal::NewerFormat`].
pub fn check_format(
    found: i64,
    records: &[StepRecord],
    this_version: &str,
) -> Result<(), RunRefusal> {
    if found <= TRACKING_FORMAT {
        return Ok(());
    }
    let last_version = records
        .iter()
        .filter(|r| !r.ferro_version.is_empty())
        .max_by(|a, b| {
            let at = |r: &StepRecord| {
                r.finished_at
                    .clone()
                    .unwrap_or_else(|| r.started_at.clone())
            };
            at(a).cmp(&at(b))
        })
        .map(|r| r.ferro_version.clone());
    Err(RunRefusal::NewerFormat {
        found,
        this_version: this_version.to_string(),
        last_version,
    })
}

/// One step's state, in `sqlx-cli migrate info`'s vocabulary (#466).
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StepState {
    /// Finished, file unchanged.
    Installed,
    /// Finished, but the file on disk changed since.
    InstalledDifferentChecksum,
    /// Recorded by `ferro migrate baseline`.
    InstalledBaseline,
    /// No finished record.
    Pending,
    /// Not finished, and a run holds the lock.
    Running,
    /// The last attempt's error recorded: an up that did not finish, or a
    /// down that failed and left the step's record standing.
    Failed,
    /// Started, not finished, no error recorded and no run holds the lock.
    Interrupted,
    /// A `down` is part-way through it.
    Reverting,
}

impl StepState {
    /// Whether a person has to look before anything runs (`status` exit 4).
    pub fn needs_attention(self) -> bool {
        matches!(
            self,
            StepState::InstalledDifferentChecksum
                | StepState::Failed
                | StepState::Interrupted
                | StepState::Reverting
        )
    }

    /// Whether the step is still to be applied (`status` exit 3).
    pub fn is_pending(self) -> bool {
        !matches!(
            self,
            StepState::Installed
                | StepState::InstalledDifferentChecksum
                | StepState::InstalledBaseline
        )
    }
}

/// One step in `ferro migrate status`.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct StepStatus {
    /// `NN`.
    pub step: u8,
    /// The file this database executes (or executed).
    pub file: String,
    /// Where it stands.
    pub state: StepState,
    /// The last attempt's error, when recorded.
    pub error: Option<String>,
    /// The recorded checksum, when there is a record.
    pub applied_checksum: Option<String>,
    /// The checksum of the file on disk.
    pub on_disk_checksum: Option<String>,
    /// `destructive` / `data-dependent` headers the file carries.
    pub flags: Vec<&'static str>,
}

/// One migration in `ferro migrate status`.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct MigrationStatus {
    /// `NNNN`.
    pub number: u16,
    /// `NNNN_<name>`.
    pub name: String,
    /// Its steps.
    pub steps: Vec<StepStatus>,
}

/// What `ferro migrate status` reports: every migration's steps, the
/// migrations the database has applied that the directory lacks, and the
/// refusal a run would meet.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct RunStatus {
    /// The directory's migrations, in order.
    pub migrations: Vec<MigrationStatus>,
    /// Applied migrations the directory lacks (`NNNN_<name>`).
    pub ahead: Vec<String>,
    /// What `up` would refuse with, when it would.
    pub refusal: Option<String>,
    /// Whether that refusal is about the database (rather than a capability
    /// this ferro lacks).
    pub refusal_needs_attention: bool,
}

/// Answer `ferro migrate status` (#466) read-only: each step's state from its
/// record, the file on disk and whether a run holds the lock, plus the
/// refusal `up` would meet. `lock_held` makes the first unfinished step
/// `running`.
pub fn run_status(
    dir: &MigrationsDir,
    records: &[StepRecord],
    dialect: Dialect,
    lock_held: bool,
) -> RunStatus {
    let by_key: RecordMap = records.iter().map(|r| ((r.migration, r.step), r)).collect();
    let mut running_marked = false;
    let mut migrations = Vec::new();
    for migration in &dir.migrations {
        let mut steps = Vec::new();
        for step in &migration.steps {
            let record = by_key.get(&(migration.number, step.ordinal));
            let file = step_file(migration, step, dialect).ok();
            let on_disk = file.map(|f| encode_checksum(&f.up_checksum));
            let name = file
                .map(|f| file_name(&f.up))
                .or_else(|| record.map(|r| r.file.clone()))
                .unwrap_or_else(|| format!("{:02}_{}", step.ordinal, step.name));
            let mut state = match record {
                None => StepState::Pending,
                Some(r) if r.reverting => StepState::Reverting,
                // A finished step carrying an error: its down failed, and the
                // record stands until a `down` reverts it.
                Some(r) if r.is_finished() && r.error.is_some() => StepState::Failed,
                Some(r) if r.is_finished() && r.origin == Origin::Baseline => {
                    StepState::InstalledBaseline
                }
                Some(r) if r.is_finished() => {
                    if Some(&r.checksum) == on_disk.as_ref() && r.file == name {
                        StepState::Installed
                    } else {
                        StepState::InstalledDifferentChecksum
                    }
                }
                Some(r) if r.error.is_some() => StepState::Failed,
                Some(_) => StepState::Interrupted,
            };
            if lock_held && !running_marked && state.is_pending() && state != StepState::Reverting {
                state = StepState::Running;
                running_marked = true;
            }
            let headers = file.map(|f| f.headers.clone()).unwrap_or_default();
            let mut flags = Vec::new();
            if headers.destructive {
                flags.push("destructive");
            }
            if headers.data_dependent {
                flags.push("data-dependent");
            }
            steps.push(StepStatus {
                step: step.ordinal,
                file: name,
                state,
                error: record.and_then(|r| r.error.clone()),
                applied_checksum: record.map(|r| r.checksum.clone()),
                on_disk_checksum: on_disk,
                flags,
            });
        }
        migrations.push(MigrationStatus {
            number: migration.number,
            name: migration.dir_name(),
            steps,
        });
    }
    let head = dir.head().map_or(0, |m| m.number);
    let ahead: BTreeSet<String> = records
        .iter()
        .filter(|r| r.migration > head)
        .map(|r| r.migration_name.clone())
        .collect();
    let refusal = plan_run(dir, records, dialect, Direction::Up, false).err();
    RunStatus {
        migrations,
        ahead: ahead.into_iter().collect(),
        refusal_needs_attention: refusal.as_ref().is_some_and(RunRefusal::needs_attention),
        refusal: refusal.map(|r| r.to_string()),
    }
}

/// The Postgres run-lock key for `governed_schema` (ADR-0029, amended by
/// ADR-0038): the first eight bytes, big-endian, of the SHA-384 of
/// `ferro-migrate-run-lock:` + the schema name. One function, because the
/// auto-migrate passes (#521) take the same lock a run takes; the namespace
/// keeps the key apart from every other advisory-lock user of the database.
pub fn run_lock_key(governed_schema: &str) -> i64 {
    let digest = sha384(format!("ferro-migrate-run-lock:{governed_schema}").as_bytes());
    let mut bytes = [0u8; 8];
    bytes.copy_from_slice(&digest[..8]);
    i64::from_be_bytes(bytes)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Lexical {
    Code,
    Single,
    Double,
    LineComment,
    BlockComment,
}

/// Cut a SQL step file into the statements the executor sends one at a time.
///
/// Not a SQL parser: a statement ends at a `;` outside a quoted string, a
/// quoted identifier, a comment and a dollar-quoted body (`DO $$ … $$`,
/// `$tag$ … $tag$`). A chunk holding only comments and whitespace (the
/// `-- ferro:` header lines, a `not-applicable` file) is no statement.
pub fn split_statements(sql: &str) -> Vec<String> {
    let mut statements = Vec::new();
    let mut current = String::new();
    let mut has_code = false;
    let mut state = Lexical::Code;
    let mut dollar: Option<String> = None;
    let chars: Vec<char> = sql.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        let next = chars.get(i + 1).copied();
        if let Some(tag) = &dollar {
            if c == '$' && sql_starts_with(&chars, i, tag) {
                current.push_str(tag);
                i += tag.chars().count();
                dollar = None;
                continue;
            }
            current.push(c);
            i += 1;
            continue;
        }
        match state {
            Lexical::Code => match c {
                ';' => {
                    if has_code {
                        statements.push(current.trim().to_string());
                    }
                    current.clear();
                    has_code = false;
                    i += 1;
                    continue;
                }
                '\'' => {
                    state = Lexical::Single;
                    has_code = true;
                }
                '"' => {
                    state = Lexical::Double;
                    has_code = true;
                }
                '-' if next == Some('-') => state = Lexical::LineComment,
                '/' if next == Some('*') => state = Lexical::BlockComment,
                '$' => {
                    if let Some(tag) = dollar_tag(&chars, i) {
                        current.push_str(&tag);
                        i += tag.chars().count();
                        dollar = Some(tag);
                        has_code = true;
                        continue;
                    }
                    has_code = true;
                }
                c if !c.is_whitespace() => has_code = true,
                _ => {}
            },
            Lexical::Single if c == '\'' => state = Lexical::Code,
            Lexical::Double if c == '"' => state = Lexical::Code,
            Lexical::LineComment if c == '\n' => state = Lexical::Code,
            Lexical::BlockComment if c == '*' && next == Some('/') => {
                current.push_str("*/");
                i += 2;
                state = Lexical::Code;
                continue;
            }
            _ => {}
        }
        current.push(c);
        i += 1;
    }
    if has_code {
        statements.push(current.trim().to_string());
    }
    statements
}

fn sql_starts_with(chars: &[char], at: usize, tag: &str) -> bool {
    let tag: Vec<char> = tag.chars().collect();
    chars.get(at..at + tag.len()) == Some(tag.as_slice())
}

/// The dollar-quote opener at `at` (`$$` or `$tag$`), when there is one.
fn dollar_tag(chars: &[char], at: usize) -> Option<String> {
    let mut end = at + 1;
    while let Some(c) = chars.get(end) {
        if *c == '$' {
            let tag: String = chars[at..=end].iter().collect();
            let inner = &tag[1..tag.len() - 1];
            let valid = inner.is_empty()
                || (inner
                    .chars()
                    .next()
                    .is_some_and(|c| c.is_alphabetic() || c == '_')
                    && inner.chars().all(|c| c.is_alphanumeric() || c == '_'));
            return valid.then_some(tag);
        }
        if !(c.is_alphanumeric() || *c == '_') {
            return None;
        }
        end += 1;
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};

    fn ir(tables: &[&str]) -> IrEnvelope<SchemaIrPayload> {
        IrEnvelope {
            ir_kind: "schema".into(),
            ir_version: 1,
            payload: SchemaIrPayload {
                dialect_agnostic: true,
                models: tables
                    .iter()
                    .map(|t| SchemaModel {
                        renamed_from: None,
                        model_name: t.to_string(),
                        table_name: t.to_string(),
                        columns: Vec::new(),
                        foreign_keys: Vec::new(),
                        indexes: Vec::new(),
                        uniques: Vec::new(),
                        checks: Vec::new(),
                        table_checks: Vec::new(),
                        row_security: None,
                    })
                    .collect(),
            },
        }
    }

    const DOWN: &str = "DROP TABLE x;\n";

    fn sql_file(dialect: &str, ordinal: u8, name: &str, body: &str, down: &str) -> StepFile {
        StepFile {
            up: PathBuf::from(format!("/m/{ordinal:02}_{name}.up.{dialect}.sql")),
            down: Some(PathBuf::from(format!(
                "/m/{ordinal:02}_{name}.down.{dialect}.sql"
            ))),
            up_checksum: sha384(body.as_bytes()),
            headers: Headers::parse(body).expect("headers"),
            down_checksum: Some(sha384(down.as_bytes())),
            down_headers: Headers::parse(down).expect("down headers"),
        }
    }

    /// A DDL step rendered for both dialects with `body`.
    fn ddl(ordinal: u8, name: &str, body: &str) -> Step {
        ddl_with_down(ordinal, name, body, DOWN)
    }

    /// A DDL step rendered for both dialects with `body` and the down `down`.
    fn ddl_with_down(ordinal: u8, name: &str, body: &str, down: &str) -> Step {
        let mut files = BTreeMap::new();
        files.insert(
            StepDialect::Postgres,
            sql_file("postgres", ordinal, name, body, down),
        );
        files.insert(
            StepDialect::Sqlite,
            sql_file("sqlite", ordinal, name, body, down),
        );
        Step {
            ordinal,
            name: name.to_string(),
            kind: StepKind::Ddl,
            files,
        }
    }

    fn data(ordinal: u8, name: &str) -> Step {
        let mut files = BTreeMap::new();
        files.insert(
            StepDialect::Portable,
            StepFile {
                up: PathBuf::from(format!("/m/{ordinal:02}_{name}.py")),
                down: None,
                up_checksum: sha384(b"# data"),
                headers: Headers::default(),
                down_checksum: None,
                down_headers: Headers::default(),
            },
        );
        Step {
            ordinal,
            name: name.to_string(),
            kind: StepKind::Data,
            files,
        }
    }

    /// Migrations `0001..` chained, each with the given steps and tables.
    fn dir(migrations: Vec<(&str, Vec<Step>, &[&str])>) -> MigrationsDir {
        let mut out: Vec<Migration> = Vec::new();
        for (i, (name, steps, tables)) in migrations.into_iter().enumerate() {
            let parent = out.last().map(|m| m.snapshot.checksum);
            let bytes = Snapshot::store(&ir(tables), parent).expect("store");
            out.push(Migration {
                number: u16::try_from(i + 1).expect("number"),
                name: name.to_string(),
                dir: PathBuf::from(format!("/migrations/{:04}_{name}", i + 1)),
                steps,
                snapshot: Snapshot::load(&bytes).expect("load"),
            });
        }
        MigrationsDir {
            path: PathBuf::from("/proj/migrations"),
            migrations: out,
        }
    }

    fn three() -> MigrationsDir {
        dir(vec![
            (
                "create_author",
                vec![ddl(1, "schema", "CREATE TABLE a (id int);\n")],
                &["author"],
            ),
            (
                "add_teams",
                vec![ddl(1, "schema", "CREATE TABLE t (id int);\n")],
                &["author", "team"],
            ),
            (
                "add_orgs",
                vec![ddl(1, "schema", "CREATE TABLE o (id int);\n")],
                &["author", "team", "org"],
            ),
        ])
    }

    /// The finished record `up` would have written for `(migration, step)`.
    fn finished(dir: &MigrationsDir, migration: u16, step: u8) -> StepRecord {
        let plan = plan_run(dir, &[], Dialect::Sqlite, Direction::Up, false).expect("plan");
        let planned = plan
            .steps
            .into_iter()
            .find(|s| s.migration == migration && s.step == step)
            .expect("planned");
        StepRecord {
            started_at: "2026-10-01T14:02:00.000000Z".into(),
            finished_at: Some("2026-10-01T14:02:33.000000Z".into()),
            ferro_version: "0.21.2".into(),
            ..planned.record
        }
    }

    fn started(dir: &MigrationsDir, migration: u16, step: u8) -> StepRecord {
        StepRecord {
            finished_at: None,
            ..finished(dir, migration, step)
        }
    }

    fn up(dir: &MigrationsDir, records: &[StepRecord]) -> Result<RunPlan, RunRefusal> {
        plan_run(dir, records, Dialect::Sqlite, Direction::Up, false)
    }

    fn keys(plan: &RunPlan) -> Vec<(u16, u8)> {
        plan.steps.iter().map(|s| (s.migration, s.step)).collect()
    }

    #[test]
    fn with_no_records_every_step_is_pending_in_order() {
        let dir = three();
        let plan = up(&dir, &[]).expect("plan");
        assert_eq!(keys(&plan), [(1, 1), (2, 1), (3, 1)]);
        let first = &plan.steps[0];
        assert_eq!(first.migration_name, "0001_create_author");
        assert_eq!(first.file, "01_schema.up.sqlite.sql");
        assert_eq!(first.mode, ExecMode::Transactional);
        assert_eq!(first.kind, RecordKind::Ddl);
        assert_eq!(
            first.checksum,
            encode_checksum(&sha384(b"CREATE TABLE a (id int);\n"))
        );
        assert_eq!(
            first.snapshot_checksum,
            encode_checksum(&dir.migrations[0].snapshot.checksum)
        );
        assert_eq!(first.record.origin, Origin::Run);
        assert!(!first.resumes && first.edited.is_none());
    }

    #[test]
    fn finished_steps_are_skipped_and_everything_applied_plans_nothing() {
        let dir = three();
        let records = vec![finished(&dir, 1, 1), finished(&dir, 2, 1)];
        assert_eq!(keys(&up(&dir, &records).expect("plan")), [(3, 1)]);
        let all = vec![
            finished(&dir, 1, 1),
            finished(&dir, 2, 1),
            finished(&dir, 3, 1),
        ];
        assert!(up(&dir, &all).expect("plan").steps.is_empty());
    }

    #[test]
    fn a_started_record_resumes_at_its_step() {
        let dir = three();
        let records = vec![finished(&dir, 1, 1), started(&dir, 2, 1)];
        let plan = up(&dir, &records).expect("plan");
        assert_eq!(keys(&plan), [(2, 1), (3, 1)]);
        assert!(plan.steps[0].resumes && plan.steps[0].edited.is_none());
    }

    #[test]
    fn an_unfinished_step_whose_file_changed_is_accepted_and_re_recorded() {
        let dir = three();
        let mut attempt = started(&dir, 2, 1);
        attempt.checksum = "0".repeat(96);
        let plan = up(&dir, &[finished(&dir, 1, 1), attempt]).expect("plan");
        let edited = plan.steps[0].edited.as_ref().expect("edited");
        assert_eq!(edited.recorded, "0".repeat(96));
        assert_eq!(plan.steps[0].record.checksum, plan.steps[0].checksum);
    }

    #[test]
    fn a_finished_step_whose_file_changed_is_refused_naming_rerecord() {
        let dir = three();
        let mut record = finished(&dir, 1, 1);
        record.checksum = "a".repeat(96);
        let refusal = up(&dir, &[record]).expect_err("edited");
        assert_eq!(refusal.kind(), "edited_applied");
        let on_disk = encode_checksum(&sha384(b"CREATE TABLE a (id int);\n"));
        assert_eq!(
            refusal.to_string(),
            format!(
                "ferro migrate: 0001_create_author/01_schema.up.sqlite.sql was edited after it \
                 was applied to this database.\n  applied   sha384:{}  (2026-10-01 14:02 UTC)\n  \
                 on disk   sha384:{on_disk}\nAn applied step is never run again. Restore the \
                 file, or accept a deliberate edit with\n`ferro migrate rerecord 0001`. Nothing \
                 was applied.",
                "a".repeat(96)
            )
        );
    }

    #[test]
    fn a_renamed_finished_file_counts_as_an_edit() {
        let dir = three();
        let mut record = finished(&dir, 1, 1);
        record.file = "01_old.up.sqlite.sql".into();
        assert_eq!(
            up(&dir, &[record]).expect_err("renamed").kind(),
            "edited_applied"
        );
    }

    #[test]
    fn a_record_under_another_snapshot_is_refused() {
        let dir = three();
        let mut record = finished(&dir, 1, 1);
        record.snapshot_checksum = "b".repeat(96);
        let refusal = up(&dir, &[record]).expect_err("snapshot");
        let on_disk = encode_checksum(&dir.migrations[0].snapshot.checksum);
        assert_eq!(
            refusal.to_string(),
            format!(
                "ferro migrate: 0001_create_author/ir.json is not the snapshot this database \
                 applied 0001_create_author under.\n  applied   sha384:{}\n  on disk   \
                 sha384:{on_disk}\nA snapshot is never edited: later migrations and historical \
                 models are built from it.\nRestore the file. Nothing was applied.",
                "b".repeat(96)
            )
        );
    }

    #[test]
    fn a_pending_migration_below_an_applied_one_is_out_of_order() {
        let dir = three();
        let records = vec![finished(&dir, 1, 1), finished(&dir, 3, 1)];
        let refusal = up(&dir, &records).expect_err("order");
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: 0002_add_teams is pending, but 0003_add_orgs is already applied to \
             this database.\nMigrations apply in order, with no override. Regenerate \
             0002_add_teams at the head\n(it becomes 0004). Nothing was applied."
        );
    }

    #[test]
    fn records_beyond_the_head_are_ahead_unless_allowed() {
        let full = three();
        let records = vec![
            finished(&full, 1, 1),
            finished(&full, 2, 1),
            finished(&full, 3, 1),
        ];
        let mut behind = full.clone();
        behind.migrations.truncate(2);
        let refusal = up(&behind, &records).expect_err("ahead");
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: this database has applied 0003_add_orgs, which is not in \
             migrations/.\nThe directory is behind the database: check out the branch that \
             holds it. Nothing was applied."
        );
        let plan =
            plan_run(&behind, &records, Dialect::Sqlite, Direction::Up, true).expect("allowed");
        assert!(plan.steps.is_empty());
        assert_eq!(plan.ahead, ["0003_add_orgs"]);
    }

    #[test]
    fn a_record_whose_migration_was_renamed_on_disk_is_missing_even_when_ahead_is_allowed() {
        let dir = three();
        let mut record = finished(&dir, 1, 1);
        record.migration_name = "0001_other".into();
        let refusal =
            plan_run(&dir, &[record], Dialect::Sqlite, Direction::Up, true).expect_err("missing");
        assert_eq!(refusal.kind(), "applied_missing");
        assert!(
            refusal
                .to_string()
                .contains("applied 0001_other, which is not in")
        );
    }

    #[test]
    fn a_reverting_record_refuses_up() {
        let dir = three();
        let mut record = finished(&dir, 1, 1);
        record.reverting = true;
        let refusal = up(&dir, &[record]).expect_err("reverting");
        assert_eq!(refusal.kind(), "reverting");
        assert!(refusal.to_string().contains("ferro migrate down"));
    }

    fn backfill() -> MigrationsDir {
        dir(vec![(
            "backfill",
            vec![ddl(1, "schema", "SELECT 1;\n"), data(2, "backfill_author")],
            &["author"],
        )])
    }

    #[test]
    fn a_pending_data_step_is_planned_for_python_to_run() {
        let dir = backfill();
        let plan = up(&dir, &[]).expect("plan");
        assert_eq!(keys(&plan), [(1, 1), (1, 2)]);
        let step = &plan.steps[1];
        assert!(step.data && !plan.steps[0].data);
        assert_eq!(step.file, "02_backfill_author.py");
        assert_eq!(step.path, PathBuf::from("/m/02_backfill_author.py"));
        assert_eq!(step.checksum, encode_checksum(&sha384(b"# data")));
        assert_eq!(step.record.checksum, step.checksum);
        assert_eq!(step.mode, ExecMode::Transactional);
        assert_eq!(step.kind, RecordKind::Atomic);
        assert_eq!(step.record.kind, RecordKind::Atomic);
    }

    #[test]
    fn a_data_step_reverts_through_its_own_file_and_an_unfinished_one_runs_nothing() {
        let dir = backfill();
        let records = [finished(&dir, 1, 1), finished(&dir, 1, 2)];
        let plan = down(&dir, &records, Target::Latest).expect("plan");
        assert_eq!(keys(&plan), [(1, 2), (1, 1)]);
        let step = &plan.steps[0];
        assert!(step.data);
        assert_eq!(step.file, "02_backfill_author.py");
        assert_eq!(step.checksum, encode_checksum(&sha384(b"# data")));
        assert_eq!(step.nothing_to_reverse, None);

        let failed = [finished(&dir, 1, 1), started(&dir, 1, 2)];
        let plan = down(&dir, &failed, Target::Latest).expect("plan");
        assert_eq!(
            plan.steps[0].nothing_to_reverse.as_deref(),
            Some(UNFINISHED_TRANSACTIONAL)
        );
    }

    fn down(
        dir: &MigrationsDir,
        records: &[StepRecord],
        target: Target,
    ) -> Result<RunPlan, RunRefusal> {
        plan_run(
            dir,
            records,
            Dialect::Sqlite,
            Direction::Down { target },
            false,
        )
    }

    /// `0001` (one step), `0002` (one step), `0003` (three steps), all applied.
    fn three_with_steps() -> (MigrationsDir, Vec<StepRecord>) {
        let dir = dir(vec![
            (
                "a",
                vec![ddl(1, "schema", "CREATE TABLE a (id int);\n")],
                &["a"],
            ),
            (
                "b",
                vec![ddl(1, "schema", "CREATE TABLE b (id int);\n")],
                &["a", "b"],
            ),
            (
                "c",
                vec![
                    ddl(1, "expand", "CREATE TABLE c (id int);\n"),
                    ddl(2, "fix", "UPDATE c SET id = 1;\n"),
                    ddl(3, "contract", "DROP TABLE b;\n"),
                ],
                &["a", "c"],
            ),
        ]);
        let records = [(1, 1), (2, 1), (3, 1), (3, 2), (3, 3)]
            .into_iter()
            .map(|(m, s)| finished(&dir, m, s))
            .collect();
        (dir, records)
    }

    #[test]
    fn down_reverts_every_target_form_newest_first() {
        let (dir, records) = three_with_steps();
        let latest = down(&dir, &records, Target::Latest).expect("latest");
        assert_eq!(keys(&latest), [(3, 3), (3, 2), (3, 1)]);
        assert_eq!(
            keys(&down(&dir, &records, Target::Migration(1)).expect("to 0001")),
            [(3, 3), (3, 2), (3, 1), (2, 1)]
        );
        assert_eq!(
            keys(&down(&dir, &records, Target::Step(3, 2)).expect("to 0003:02")),
            [(3, 3)]
        );
        let all = [(3, 3), (3, 2), (3, 1), (2, 1), (1, 1)];
        assert_eq!(keys(&down(&dir, &records, Target::All).expect("all")), all);
        assert_eq!(
            keys(&down(&dir, &records, Target::Migration(0)).expect("to 0000")),
            all
        );
        assert!(
            down(&dir, &records, Target::Migration(3))
                .expect("at head")
                .steps
                .is_empty()
        );
        assert!(
            down(&dir, &[], Target::Latest)
                .expect("nothing")
                .steps
                .is_empty()
        );
    }

    #[test]
    fn a_planned_down_runs_the_down_file_and_removes_the_standing_record() {
        let (dir, records) = three_with_steps();
        let plan = down(&dir, &records, Target::Latest).expect("plan");
        let step = &plan.steps[0];
        assert_eq!(step.file, "03_contract.down.sqlite.sql");
        assert_eq!(step.path, PathBuf::from("/m/03_contract.down.sqlite.sql"));
        assert_eq!(step.checksum, encode_checksum(&sha384(DOWN.as_bytes())));
        assert_eq!(step.mode, ExecMode::Transactional);
        assert_eq!(step.record, records[4]);
        assert_eq!(step.nothing_to_reverse, None);
    }

    #[test]
    fn a_partly_applied_migration_is_the_latest_and_an_unfinished_transactional_step_runs_nothing()
    {
        let (dir, mut records) = three_with_steps();
        records.truncate(3);
        records.push(started(&dir, 3, 2));
        let plan = down(&dir, &records, Target::Latest).expect("plan");
        assert_eq!(keys(&plan), [(3, 2), (3, 1)]);
        assert_eq!(
            plan.steps[0].nothing_to_reverse.as_deref(),
            Some(UNFINISHED_TRANSACTIONAL)
        );
        assert_eq!(plan.steps[1].nothing_to_reverse, None);
    }

    #[test]
    fn a_nothing_to_reverse_down_is_planned_with_its_reason() {
        let dir = dir(vec![(
            "a",
            vec![ddl_with_down(
                1,
                "schema",
                "CREATE TABLE a (id int);\n",
                "-- ferro: nothing-to-reverse the label stays\n",
            )],
            &["a"],
        )]);
        let plan = down(&dir, &[finished(&dir, 1, 1)], Target::Latest).expect("plan");
        assert_eq!(
            plan.steps[0].nothing_to_reverse.as_deref(),
            Some("the label stays")
        );
    }

    #[test]
    fn an_irreversible_step_refuses_the_whole_down_quoting_its_reason() {
        let dir = dir(vec![
            (
                "a",
                vec![ddl(1, "schema", "CREATE TABLE a (id int);\n")],
                &["a"],
            ),
            (
                "b",
                vec![
                    ddl(1, "expand", "CREATE TABLE b (id int);\n"),
                    ddl_with_down(
                        2,
                        "purge",
                        "DELETE FROM b;\n",
                        "-- ferro: irreversible dropped rows cannot come back\n",
                    ),
                    ddl(3, "index", "CREATE INDEX i ON b (id);\n"),
                ],
                &["a", "b"],
            ),
        ]);
        let records: Vec<StepRecord> = [(1, 1), (2, 1), (2, 2), (2, 3)]
            .into_iter()
            .map(|(m, s)| finished(&dir, m, s))
            .collect();
        let refusal = down(&dir, &records, Target::Latest).expect_err("irreversible");
        assert_eq!(
            refusal,
            RunRefusal::Irreversible {
                migration: 2,
                step: 2,
                reason: "dropped rows cannot come back".into(),
            }
        );
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: 0002:02 is irreversible: dropped rows cannot come back\nThere is \
             no flag to skip it: to revert past it, write the step's down in place of the \
             declaration. Nothing was reverted."
        );
        // Stopping above it is fine: only the steps a run reverts are read.
        assert_eq!(
            keys(&down(&dir, &records, Target::Step(2, 2)).expect("above")),
            [(2, 3)]
        );
    }

    #[test]
    fn down_stops_at_a_baselined_migration() {
        let (dir, mut records) = three_with_steps();
        records[0].origin = Origin::Baseline;
        records[1].origin = Origin::Baseline;
        assert_eq!(
            keys(&down(&dir, &records, Target::Migration(2)).expect("above the floor")),
            [(3, 3), (3, 2), (3, 1)]
        );
        let refusal = down(&dir, &records, Target::All).expect_err("below");
        assert_eq!(refusal, RunRefusal::BelowBaseline { migration: 2 });
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: 0002 was recorded by `baseline` and created nothing here; `down` \
             can go no lower than 0002. Nothing was reverted."
        );
        assert_eq!(
            down(&dir, &records, Target::Migration(1))
                .expect_err("below")
                .kind(),
            "below_baseline"
        );
    }

    #[test]
    fn a_target_the_directory_lacks_is_refused_naming_it() {
        let (dir, records) = three_with_steps();
        for (target, shown) in [
            (Target::Migration(9), "0009"),
            (Target::Step(3, 4), "0003:04"),
            (Target::Step(9, 1), "0009:01"),
        ] {
            let refusal = down(&dir, &records, target).expect_err("no such");
            assert_eq!(refusal.kind(), "no_such_target");
            assert!(
                refusal.to_string().starts_with(&format!(
                    "ferro migrate: --to {shown} names nothing in migrations/"
                )),
                "{refusal}"
            );
        }
    }

    #[test]
    fn a_database_ahead_of_the_checkout_cannot_go_down() {
        let (dir, records) = three_with_steps();
        let mut ahead = records.clone();
        ahead.push(StepRecord {
            migration: 4,
            migration_name: "0004_gone".into(),
            ..records[0].clone()
        });
        let refusal = plan_run(
            &dir,
            &ahead,
            Dialect::Sqlite,
            Direction::Down {
                target: Target::Latest,
            },
            true,
        )
        .expect_err("ahead");
        assert_eq!(refusal.kind(), "applied_missing");
    }

    #[test]
    fn targets_read_and_write_the_json_the_cli_sends() {
        let parse = |json: &str| serde_json::from_str::<Direction>(json).expect(json);
        assert_eq!(parse(r#"{"direction": "up"}"#), Direction::Up);
        for (json, target) in [
            (r#""latest""#, Target::Latest),
            (r#""all""#, Target::All),
            (r#"{"migration": 5}"#, Target::Migration(5)),
            (r#"{"step": [7, 2]}"#, Target::Step(7, 2)),
        ] {
            assert_eq!(
                parse(&format!(r#"{{"direction": "down", "target": {json}}}"#)),
                Direction::Down { target }
            );
        }
    }

    #[test]
    fn a_finished_step_carrying_an_error_is_a_failed_down() {
        let (dir, mut records) = three_with_steps();
        records[4].error = Some("relation \"b\" does not exist".into());
        records[4].failed_at = Some("2026-10-06T10:00:00.000000Z".into());
        let status = run_status(&dir, &records, Dialect::Sqlite, false);
        let states: Vec<StepState> = status.migrations[2].steps.iter().map(|s| s.state).collect();
        assert_eq!(
            states,
            [
                StepState::Installed,
                StepState::Installed,
                StepState::Failed
            ]
        );
        assert_eq!(
            keys(&down(&dir, &records, Target::Latest).expect("resume")),
            [(3, 3), (3, 2), (3, 1)]
        );
    }

    #[test]
    fn header_modes_per_dialect() {
        let headers = |text: &str| Headers::parse(text).expect("headers");
        let plain = headers("SELECT 1;\n");
        let no_tx = headers("-- ferro: no-transaction\n");
        let fk_off = headers("-- ferro: foreign-keys-off\n");
        let both = headers("-- ferro: no-transaction\n-- ferro: foreign-keys-off\n");
        assert_eq!(
            exec_mode(&plain, Dialect::Postgres, "f"),
            Ok(ExecMode::Transactional)
        );
        assert_eq!(
            exec_mode(&no_tx, Dialect::Postgres, "f"),
            Ok(ExecMode::NoTransaction)
        );
        assert_eq!(
            exec_mode(&fk_off, Dialect::Sqlite, "f"),
            Ok(ExecMode::ForeignKeysOff)
        );
        for (h, d) in [
            (&no_tx, Dialect::Sqlite),
            (&fk_off, Dialect::Postgres),
            (&both, Dialect::Sqlite),
        ] {
            assert_eq!(exec_mode(h, d, "f").expect_err("bad").kind(), "bad_headers");
        }
        assert_eq!(
            ExecMode::NoTransaction.record_kind(),
            RecordKind::DdlNoTransaction
        );
        assert_eq!(ExecMode::ForeignKeysOff.record_kind(), RecordKind::Ddl);

        let dir = dir(vec![(
            "idx",
            vec![
                ddl(1, "schema", "SELECT 1;\n"),
                ddl(
                    2,
                    "index",
                    "-- ferro: no-transaction\nCREATE INDEX CONCURRENTLY i ON t (x);\n",
                ),
            ],
            &["t"],
        )]);
        let plan = plan_run(&dir, &[], Dialect::Postgres, Direction::Up, false).expect("plan");
        assert_eq!(plan.steps[1].mode, ExecMode::NoTransaction);
        assert_eq!(plan.steps[1].record.kind, RecordKind::DdlNoTransaction);
    }

    #[test]
    fn a_ddl_step_without_this_dialects_rendering_is_refused() {
        let mut dir = three();
        dir.migrations[1].steps[0]
            .files
            .remove(&StepDialect::Sqlite);
        let refusal = up(&dir, &[]).expect_err("rendering");
        assert_eq!(
            refusal.to_string(),
            "0002 step 01 has no sqlite rendering. This database is sqlite; the migration was \
             generated for: postgres. Add \"sqlite\" to dialects and regenerate, or write \
             01_schema.up.sqlite.sql."
        );
    }

    #[test]
    fn steps_number_densely_within_a_migration() {
        let dir = dir(vec![(
            "multi",
            vec![
                ddl(1, "a", "SELECT 1;\n"),
                ddl(2, "b", "SELECT 2;\n"),
                ddl(3, "c", "SELECT 3;\n"),
            ],
            &["t"],
        )]);
        let records = vec![finished(&dir, 1, 1)];
        assert_eq!(keys(&up(&dir, &records).expect("plan")), [(1, 2), (1, 3)]);
    }

    #[test]
    fn adoption_refuses_a_live_table_of_the_first_snapshot_only_without_records() {
        let dir = three();
        let live = vec!["author".to_string(), "unrelated".to_string()];
        let refusal = check_adoption(&dir, &[], &live).expect_err("baseline");
        assert_eq!(
            refusal.to_string(),
            "This database has no ferro migration records, but table \"author\" from 0001 \
             already exists. If it was built by auto-migrate or Alembic, run \"ferro migrate \
             baseline 0001\". up will not run 0001 over it."
        );
        assert!(check_adoption(&dir, &[finished(&dir, 1, 1)], &live).is_ok());
        assert!(check_adoption(&dir, &[], &["unrelated".to_string()]).is_ok());
    }

    #[test]
    fn a_newer_format_is_refused_naming_the_last_ferro() {
        let dir = three();
        assert!(check_format(1, &[], "0.21.2").is_ok());
        let mut record = finished(&dir, 1, 1);
        record.ferro_version = "0.30.0".into();
        let refusal = check_format(99, &[record], "0.21.2").expect_err("newer");
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: this database's tracking table is format 99; this ferro (0.21.2) \
             understands format 1.\nIt was last migrated by ferro 0.30.0. Upgrade ferro to run \
             migrations against it."
        );
    }

    #[test]
    fn a_broken_chain_shows_both_checksums_aligned() {
        let refusal = RunRefusal::BrokenChain {
            child: "0008_drop_legacy".into(),
            parent: Some("0007_nickname".into()),
            expected: Some("91c0".into()),
            actual: Some("0d44".into()),
        };
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: the migration chain is broken at 0008_drop_legacy.\n  \
             0008_drop_legacy/ir.json expects parent   sha384:91c0\n  \
             0007_nickname/ir.json is                  sha384:0d44\n0007_nickname's snapshot \
             changed after 0008_drop_legacy was generated. Restore it, or\nregenerate \
             0008_drop_legacy. Nothing was applied."
        );
    }

    #[test]
    fn read_for_run_reports_a_broken_chain_with_its_checksums() {
        let root =
            std::env::temp_dir().join(format!("ferro-run-plan-chain-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let write = |path: PathBuf, bytes: &[u8]| {
            std::fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
            std::fs::write(path, bytes).expect("write");
        };
        let first = Snapshot::store(&ir(&["a"]), None).expect("store");
        let second = Snapshot::store(&ir(&["a", "b"]), Some([7u8; 48])).expect("store");
        for (dir, snapshot) in [("0001_a", &first), ("0002_b", &second)] {
            write(root.join(dir).join("01_s.up.sqlite.sql"), b"SELECT 1;\n");
            write(root.join(dir).join("01_s.down.sqlite.sql"), b"SELECT 1;\n");
            write(root.join(dir).join("ir.json"), snapshot);
        }
        let refusal = read_for_run(&root).expect_err("chain");
        assert_eq!(
            refusal,
            RunRefusal::BrokenChain {
                child: "0002_b".into(),
                parent: Some("0001_a".into()),
                expected: Some(encode_checksum(&[7u8; 48])),
                actual: Some(encode_checksum(&sha384(&first))),
            }
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn status_reports_each_state() {
        let dir = dir(vec![
            ("one", vec![ddl(1, "schema", "SELECT 1;\n")], &["a"]),
            (
                "two",
                vec![
                    ddl(1, "a", "SELECT 1;\n"),
                    ddl(2, "b", "-- ferro: destructive\nSELECT 2;\n"),
                    ddl(3, "c", "SELECT 3;\n"),
                ],
                &["a"],
            ),
        ]);
        let mut failed = started(&dir, 2, 2);
        failed.error = Some("no such table: x".into());
        let records = vec![finished(&dir, 1, 1), finished(&dir, 2, 1), failed];
        let status = run_status(&dir, &records, Dialect::Sqlite, false);
        let states: Vec<Vec<StepState>> = status
            .migrations
            .iter()
            .map(|m| m.steps.iter().map(|s| s.state).collect())
            .collect();
        assert_eq!(
            states,
            [
                vec![StepState::Installed],
                vec![StepState::Installed, StepState::Failed, StepState::Pending]
            ]
        );
        assert_eq!(
            status.migrations[1].steps[1].error.as_deref(),
            Some("no such table: x")
        );
        assert_eq!(status.migrations[1].steps[1].flags, ["destructive"]);
        assert!(status.refusal.is_none());

        let interrupted = vec![finished(&dir, 1, 1), started(&dir, 2, 1)];
        let status = run_status(&dir, &interrupted, Dialect::Sqlite, false);
        assert_eq!(status.migrations[1].steps[0].state, StepState::Interrupted);
        let status = run_status(&dir, &interrupted, Dialect::Sqlite, true);
        assert_eq!(status.migrations[1].steps[0].state, StepState::Running);
        assert_eq!(status.migrations[1].steps[1].state, StepState::Pending);

        let mut baseline = finished(&dir, 1, 1);
        baseline.origin = Origin::Baseline;
        let mut edited = finished(&dir, 2, 1);
        edited.checksum = "c".repeat(96);
        let status = run_status(&dir, &[baseline, edited], Dialect::Sqlite, false);
        assert_eq!(
            status.migrations[0].steps[0].state,
            StepState::InstalledBaseline
        );
        assert_eq!(
            status.migrations[1].steps[0].state,
            StepState::InstalledDifferentChecksum
        );
        assert!(
            status
                .refusal
                .as_deref()
                .is_some_and(|r| r.contains("rerecord"))
        );
        assert!(status.refusal_needs_attention);
    }

    #[test]
    fn the_run_lock_key_is_stable_and_per_schema() {
        assert_eq!(run_lock_key("public"), run_lock_key("public"));
        assert_ne!(run_lock_key("public"), run_lock_key("tenant"));
        let digest = sha384(b"ferro-migrate-run-lock:public");
        let mut bytes = [0u8; 8];
        bytes.copy_from_slice(&digest[..8]);
        assert_eq!(run_lock_key("public"), i64::from_be_bytes(bytes));
    }

    #[test]
    fn the_splitter_keeps_dollar_bodies_quotes_and_comments_whole() {
        let sql = "-- ferro: destructive\n\nDO $$ BEGIN\n  CREATE TYPE \"s\" AS ENUM ('a;b');\n\
                   EXCEPTION WHEN duplicate_object THEN NULL; END $$;\n\n\
                   CREATE TABLE \"t;x\" (\"id\" integer); -- trailing; comment\n\
                   /* block; */ SELECT $tag$ ; $tag$;\nSELECT 'it''s; fine'";
        assert_eq!(
            split_statements(sql),
            [
                "-- ferro: destructive\n\nDO $$ BEGIN\n  CREATE TYPE \"s\" AS ENUM ('a;b');\n\
                 EXCEPTION WHEN duplicate_object THEN NULL; END $$",
                "CREATE TABLE \"t;x\" (\"id\" integer)",
                "-- trailing; comment\n/* block; */ SELECT $tag$ ; $tag$",
                "SELECT 'it''s; fine'",
            ]
        );
        assert!(split_statements("-- ferro: not-applicable\n").is_empty());
        assert!(split_statements("\n  ;\n").is_empty());
    }
}
