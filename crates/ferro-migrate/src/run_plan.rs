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
//! its tracking table's format number). [`adoption`] is what a run adopts
//! before it plans: each squash whose witness the records hold
//! ([`adoptable`]), refused on one they hold part of ([`partial_witnesses`],
//! ADR-0056). [`split_statements`] cuts a step file
//! into the statements the executor sends one at a time, and
//! [`run_lock_key`] is the one derivation of the Postgres run-lock key.

pub mod step_class;

use crate::Dialect;
use crate::directory::{
    DirectoryError, Headers, Migration, MigrationsDir, SNAPSHOT_FILE, Step, StepDialect, StepFile,
    StepKind,
};
use crate::generate::rebuild::rebuilt_tables;
use crate::plan::{Hint, and_list, live_hints, reverse_hints};
use crate::snapshot::{Snapshot, WitnessEntry, encode_checksum, sha384};
use ferro_schema_ir::{SchemaIrPayload, SchemaModel};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
pub use step_class::StepClass;
use step_class::migration_classes;

/// The tracking table format this ferro reads and writes: a reshaping an
/// older ferro refuses bumps it (ADR-0030 as amended). It stays 1.
pub const TRACKING_FORMAT: i64 = 1;

/// The tracking table's minor: each additive change within a format, a
/// nullable column an older ferro reads, writes and re-records beside
/// unchanged (ADR-0030 as amended). Minor 1 is the step class column
/// (ADR-0054). A newer ferro's first mutating verb on a lower minor adds
/// what is missing under the run lock and records the minor; readers never
/// write.
pub const TRACKING_MINOR: i64 = 1;

/// How far `up` and `require_applied` let a database ahead of the
/// directory through (ADR-0038, ADR-0054). As JSON and from Python: `false`,
/// `"expand-only"`, `true`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum AllowAhead {
    /// Refuse any applied migration the directory lacks (`False`).
    #[default]
    No,
    /// Let them through when every step record the directory lacks is of
    /// class `expand` (`"expand-only"`).
    ExpandOnly,
    /// Let any distance through (`True`).
    Any,
}

impl From<bool> for AllowAhead {
    fn from(allow: bool) -> Self {
        if allow {
            AllowAhead::Any
        } else {
            AllowAhead::No
        }
    }
}

impl AllowAhead {
    /// The spelling `"expand-only"` takes in every door.
    pub const EXPAND_ONLY: &'static str = "expand-only";
}

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
    /// A run adopted it: the database had applied the migrations a squash
    /// replaced, and the run swapped their records for the squash's,
    /// running nothing (ADR-0056).
    Adopted,
}

impl Origin {
    /// The spelling stored in the `origin` column.
    pub fn as_str(self) -> &'static str {
        match self {
            Origin::Run => "run",
            Origin::Baseline => "baseline",
            Origin::Adopted => "adopted",
        }
    }

    /// Whether a baseline recorded it. Every rule written for origin `run`
    /// means "not baseline": an adopted record stands for steps a run
    /// applied, so it blocks `baseline --remove` and its `down` runs.
    pub fn is_baseline(self) -> bool {
        self == Origin::Baseline
    }

    /// The origin the `origin` column spells.
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "run" => Some(Origin::Run),
            "baseline" => Some(Origin::Baseline),
            "adopted" => Some(Origin::Adopted),
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
    /// Ran, recorded by a baseline, or adopted from a squash's originals.
    pub origin: Origin,
    /// A chunked `down` is part-way (declared by #520; #532 drives it).
    #[serde(default)]
    pub reverting: bool,
    /// A chunked `down`'s own cursor (declared by #520; #532 drives it).
    #[serde(default)]
    pub revert_cursor: Option<String>,
    /// The step's class (ADR-0054), recorded when the step is applied by a
    /// run or a baseline; `None` for a record an older ferro wrote, read as
    /// `contract` until a mutating verb classifies it ([`unclassified`]).
    #[serde(default)]
    pub class: Option<StepClass>,
}

impl StepRecord {
    /// The step's class as the record says it: `contract` when it says
    /// nothing (written by an older ferro).
    pub fn class_or_contract(&self) -> StepClass {
        self.class.unwrap_or(StepClass::Contract)
    }

    /// `NNNN_<name>:NN`, as `DatabaseAheadError.contract_steps` names a step.
    pub fn address(&self) -> String {
        format!("{}:{:02}", self.migration_name, self.step)
    }

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

/// Which way a run goes. As JSON: `{"direction": "up"}`, `{"direction":
/// "up", "through": 7}`, `{"direction": "down", "target": ...}`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(tag = "direction", rename_all = "lowercase")]
pub enum Direction {
    /// Apply every pending step; with `through`, only the pending steps of
    /// migrations up to and including that one (the test harness's target,
    /// ADR-0045). A `through` the directory lacks is refused; one at or
    /// below the last applied migration plans nothing.
    Up {
        /// The last migration to apply (`0007` is `7`), from
        /// [`parse_through`].
        #[serde(default)]
        through: Option<u16>,
    },
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

/// `up`'s `through` as the operator wrote it: a migration number (`"0007"`).
///
/// # Errors
/// [`RunRefusal::NotAMigration`] for anything else, a step address
/// (`"0007:02"`) included: no snapshot describes the state between two
/// steps.
pub fn parse_through(text: &str) -> Result<u16, RunRefusal> {
    let trimmed = text.trim();
    let not_a_migration = || RunRefusal::NotAMigration {
        target: trimmed.to_string(),
    };
    if trimmed.len() != 4 || !trimmed.bytes().all(|b| b.is_ascii_digit()) {
        return Err(not_a_migration());
    }
    trimmed.parse().map_err(|_| not_a_migration())
}

/// One table a SQLite `foreign-keys-off` step rebuilds (ADR-0034), as the
/// step's own bytes say it does and its migration's two snapshots declare
/// it: what the executor compares with the live table before the step runs.
#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct RebuildExpectation {
    /// The table the step rebuilds (`CREATE TABLE "_ferro_new_<table>"`).
    pub table: String,
    /// Its name before the step runs: a step that renames a table rebuilds
    /// it under its new name, after the rename (ADR-0032, ADR-0046).
    pub starting_name: String,
    /// The table as the snapshot the file starts from declares it, with
    /// every column the migration's other snapshot declares added: a later
    /// step of the migration (a contract after its expand) finds the table
    /// as an earlier step left it, so a column either side declares is
    /// never one the rebuild discards unannounced (ADR-0025).
    pub declared: SchemaModel,
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
    /// The ordinal of the migration's first data step, when it has one: the
    /// backfill a failed contract's recipe re-runs (`down --to` the step
    /// before it, then `up`).
    #[serde(default)]
    pub first_data_step: Option<u8>,
    /// A SQLite `foreign-keys-off` step's rebuilt tables, read off the step
    /// file's own bytes by [`HeldDirectory::plan`]. Those bytes are what
    /// runs, and an unfinished step's file may have been edited (ADR-0030),
    /// so nothing else is the source. Empty for every other step, and from
    /// [`plan_run`], which sees no bytes.
    #[serde(default)]
    pub rebuilds: Vec<RebuildExpectation>,
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
        /// `NN`.
        step: u8,
        /// The file the record names.
        file: String,
        /// The recorded checksum.
        applied: String,
        /// When it finished (ISO-8601).
        applied_at: String,
        /// The checksum of the file on disk (`None` when the file was renamed
        /// away or deleted).
        on_disk: Option<String>,
        /// Whether a `down` met it (nothing was reverted) rather than an
        /// `up` (nothing was applied).
        down: bool,
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
        /// Whether `allow_ahead` alone would have let the run through: every
        /// missing migration sorts above the directory's head, and the run
        /// meets no other refusal. The application's `up()` raises
        /// `DatabaseAheadError` on it, from this one answer.
        ahead_only: bool,
        /// Each step record of an ahead migration whose class is not
        /// `expand` (a missing class is `contract`), `NNNN_<name>:NN` in
        /// order: what `allow_ahead="expand-only"` refuses on.
        contract_steps: Vec<String>,
        /// Whether the run asked for `allow_ahead="expand-only"`: the text
        /// then names the first contract step that refused it.
        expand_only: bool,
        /// The lowest number at or below the directory's head whose applied
        /// migration (or step) the directory lacks: a migration replaced
        /// after this database applied it (`ferro migrate new --replace`)
        /// looks exactly like this, so the text names that cause and its
        /// revert beside the checkout.
        replaced: Option<u16>,
    },
    /// `up`'s `through` names a migration the directory does not hold.
    NoSuchMigration {
        /// `NNNN`.
        migration: u16,
        /// The directory, as configured.
        directory: String,
    },
    /// `up`'s `through` is not a migration number: a step address
    /// (`0007:02`) or anything else.
    NotAMigration {
        /// The target as written.
        target: String,
    },
    /// A SQL step file the run would execute cannot be read as it was
    /// hashed: it changed while the directory was read, or is not UTF-8.
    Unreadable {
        /// `NNNN_<name>/<file>`, or the path when no migration names it.
        file: String,
        /// Why.
        reason: String,
    },
    /// A SQLite `foreign-keys-off` step whose rebuilt tables cannot be
    /// planned: one the snapshot it starts from does not declare, or a
    /// rename hint the generator refuses (a hand-edited `ir.json`).
    Rebuild {
        /// `NNNN_<name>/<file>`.
        file: String,
        /// What is wrong, as the end of a sentence about the file.
        reason: String,
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
    /// An unfinished chunked step with committed batches whose file changed
    /// (#466 "edited chunked step with committed batches", ADR-0030): ferro
    /// cannot tell whether the committed rows are right under the new code,
    /// so the developer chooses `rerecord --continue` or `--restart`.
    EditedChunked {
        /// `NNNN_<name>`.
        migration_name: String,
        /// `NNNN`.
        migration: u16,
        /// `NN`.
        step: u8,
        /// The step's file on disk.
        file: String,
        /// The checksum the unfinished attempt recorded.
        recorded: String,
        /// The checksum of the file on disk.
        on_disk: String,
        /// The rows its committed batches wrote.
        rows_done: i64,
        /// Whether `--continue` is a door: the cursor records its order keys
        /// and the edited file (when read) pages over the same ones.
        continue_allowed: bool,
        /// The order keys the cursor was committed under (`author.id`);
        /// empty when the cursor does not record them.
        keys_recorded: Vec<String>,
        /// The order keys the edited file pages over; `Some([])` when its
        /// `up` is not `@chunked`, `None` when the caller did not read them.
        keys_on_disk: Option<Vec<String>>,
    },
    /// A chunked step whose `down` stopped part-way (its record
    /// `reverting`) was edited so its `down` no longer pages over the order
    /// keys its revert cursor was committed under (ADR-0030): the cursor is
    /// no position in the edited query, so only `rerecord --restart` goes on.
    EditedReverting {
        /// `NNNN_<name>`.
        migration_name: String,
        /// `NNNN`.
        migration: u16,
        /// `NN`.
        step: u8,
        /// The step's file on disk.
        file: String,
        /// The rows the down's committed batches reverted.
        rows_done: i64,
        /// The order keys the revert cursor was committed under; empty when
        /// it does not record them.
        keys_recorded: Vec<String>,
        /// The order keys the edited `down` pages over; empty when it is not
        /// `@chunked`.
        keys_on_disk: Vec<String>,
    },
    /// `rerecord <target>` names no single step: a migration alone, the
    /// snapshot, or something that is not `<migration>:<step>`.
    RerecordTarget {
        /// The target as the operator wrote it.
        target: String,
    },
    /// `rerecord` names a step with nothing to re-record.
    NothingToRerecord {
        /// `NNNN:NN`.
        target: String,
        /// Why, as the end of a sentence.
        why: String,
    },
    /// `rerecord --continue` / `--restart` on a step they do not apply to.
    ModeNotApplicable {
        /// `NNNN:NN`.
        target: String,
        /// `--continue` or `--restart`.
        flag: &'static str,
        /// What the step is (`finished`, `an unfinished atomic step`, ...).
        state: String,
    },
    /// `rerecord --continue` on a step whose edited file pages over
    /// different order keys than its cursor was committed under.
    ContinueRefused {
        /// `NNNN_<name>`.
        migration_name: String,
        /// `NNNN:NN`.
        target: String,
        /// The step's file on disk.
        file: String,
        /// The cursor's order keys (empty when it records none).
        keys_recorded: Vec<String>,
        /// The edited file's order keys (empty when its `up` is not chunked).
        keys_on_disk: Vec<String>,
    },
    /// `baseline` on a tracked database: its tracking table already holds
    /// step records, and a baseline records only on a database that has
    /// none (ADR-0031). The second of two concurrent pre-deploys meets it
    /// once the first has recorded; the application's `baseline()` raises
    /// `AlreadyTrackedError` on it, by this kind.
    AlreadyTracked {
        /// Every migration a record names (`NNNN_<name>`), in order.
        applied: Vec<String>,
        /// The last of them.
        head: String,
    },
    /// `baseline --remove` under a run that applied a migration above the
    /// baseline: removing it would leave applied migrations above pending
    /// ones. The application's `remove_baseline()` raises
    /// `AppliedAboveBaselineError` on it, by this kind.
    AppliedAboveBaseline {
        /// Each run-applied migration above the baseline (`NNNN_<name>`).
        above: Vec<String>,
        /// The highest baselined migration (`NNNN_<name>`): the floor
        /// `down --to` reverts to.
        floor: String,
    },
    /// `baseline` over a migrations directory that holds no migration.
    NothingToBaseline {
        /// The directory's name (`migrations`).
        directory: String,
    },
    /// `baseline <target>` names no migration the directory holds.
    NoBaselineTarget {
        /// The target as the operator wrote it.
        target: String,
        /// The directory's name (`migrations`).
        directory: String,
        /// `NNNN_<name>` of the directory's head.
        head: String,
    },
    /// A step `baseline` would record that no run could execute on this
    /// dialect (a missing rendering, headers it cannot honour): the run
    /// planner's own refusal, said as a baseline's.
    BaselineStep {
        /// The run planner's refusal for the step.
        refusal: Box<RunRefusal>,
    },
    /// A migration whose generated steps need a class (a run applying one,
    /// a baseline recording one, a record an older ferro wrote) and that does
    /// not regenerate from its two snapshots, so the planner decides no
    /// class for them (ADR-0054): none is guessed.
    Unclassifiable {
        /// `NNNN`.
        migration: u16,
        /// `NNNN_<name>`.
        migration_name: String,
        /// The generator's refusal.
        error: String,
    },
    /// A squash whose witness the tracking table holds only part of
    /// (ADR-0056): the database applied some of the migrations it replaced
    /// and not the rest, or a step of one did not finish. It is not adopted,
    /// and nothing runs: the database stands at no snapshot the directory
    /// holds.
    PartialWitness {
        /// `NNNN_<name>` of the squash.
        squash: String,
        /// `NNNN_<name>` of the migration below it, `0000` when it is the
        /// first: where the revert goes.
        parent: String,
        /// The replaced migrations the records hold whole, in chain order.
        satisfied: Vec<String>,
        /// The ones they do not, in chain order.
        missing: Vec<String>,
    },
}

/// `NNNN` of a migration's `NNNN_<name>`.
fn number_of(migration_name: &str) -> &str {
    migration_name
        .split_once('_')
        .map_or(migration_name, |(number, _)| number)
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
            RunRefusal::EditedChunked { .. } => "edited_chunked",
            RunRefusal::EditedReverting { .. } => "edited_reverting",
            RunRefusal::RerecordTarget { .. } => "rerecord_target",
            RunRefusal::NothingToRerecord { .. } => "nothing_to_rerecord",
            RunRefusal::ModeNotApplicable { .. } => "mode_not_applicable",
            RunRefusal::ContinueRefused { .. } => "continue_refused",
            RunRefusal::NoSuchMigration { .. } => "no_such_migration",
            RunRefusal::NotAMigration { .. } => "not_a_migration",
            RunRefusal::Unreadable { .. } => "unreadable",
            RunRefusal::Rebuild { .. } => "rebuild",
            RunRefusal::AlreadyTracked { .. } => "already_tracked",
            RunRefusal::AppliedAboveBaseline { .. } => "applied_above_baseline",
            RunRefusal::NothingToBaseline { .. } => "nothing_to_baseline",
            RunRefusal::NoBaselineTarget { .. } => "no_baseline_target",
            RunRefusal::BaselineStep { .. } => "baseline_step",
            RunRefusal::Unclassifiable { .. } => "unclassifiable",
            RunRefusal::PartialWitness { .. } => "partial_witness",
        }
    }

    /// The migrations the refusal names (`NNNN_<name>`), when it is about a
    /// set of them: a tracked database's records
    /// ([`RunRefusal::AlreadyTracked`]) or the run-applied migrations above
    /// a baseline ([`RunRefusal::AppliedAboveBaseline`]).
    pub fn names(&self) -> Option<&[String]> {
        match self {
            RunRefusal::AlreadyTracked { applied, .. } => Some(applied),
            RunRefusal::AppliedAboveBaseline { above, .. } => Some(above),
            _ => None,
        }
    }

    /// The newest migration a tracked database's records name
    /// ([`RunRefusal::AlreadyTracked`]'s `head`), `NNNN_<name>`.
    pub fn head(&self) -> Option<&str> {
        match self {
            RunRefusal::AlreadyTracked { head, .. } => Some(head),
            _ => None,
        }
    }

    /// The ahead step records whose class is not `expand`
    /// ([`RunRefusal::AppliedMissing`]'s `contract_steps`), `NNNN_<name>:NN`.
    pub fn contract_steps(&self) -> &[String] {
        match self {
            RunRefusal::AppliedMissing { contract_steps, .. } => contract_steps,
            _ => &[],
        }
    }

    /// Whether `allow_ahead` alone would have let the run through
    /// ([`RunRefusal::AppliedMissing`]'s `ahead_only`).
    pub fn ahead_only(&self) -> bool {
        matches!(
            self,
            RunRefusal::AppliedMissing {
                ahead_only: true,
                ..
            }
        )
    }

    /// The migration the refusal is about, when it names one.
    pub fn migration(&self) -> Option<u16> {
        match self {
            RunRefusal::EditedApplied { migration, .. }
            | RunRefusal::NoSuchMigration { migration, .. }
            | RunRefusal::TablesExist { migration, .. }
            | RunRefusal::Irreversible { migration, .. }
            | RunRefusal::BelowBaseline { migration }
            | RunRefusal::MissingRendering { migration, .. }
            | RunRefusal::EditedChunked { migration, .. }
            | RunRefusal::EditedReverting { migration, .. }
            | RunRefusal::Unclassifiable { migration, .. } => Some(*migration),
            RunRefusal::BaselineStep { refusal } => refusal.migration(),
            _ => None,
        }
    }

    /// The step the refusal is about, when it names one.
    pub fn step(&self) -> Option<u8> {
        match self {
            RunRefusal::EditedApplied { step, .. }
            | RunRefusal::Irreversible { step, .. }
            | RunRefusal::MissingRendering { step, .. }
            | RunRefusal::EditedChunked { step, .. }
            | RunRefusal::EditedReverting { step, .. } => Some(*step),
            RunRefusal::BaselineStep { refusal } => refusal.step(),
            _ => None,
        }
    }

    /// The declared or diagnosed reason the refusal carries, when it has one
    /// apart from its text (an irreversible step's declared reason).
    pub fn reason(&self) -> Option<&str> {
        match self {
            RunRefusal::Irreversible { reason, .. } | RunRefusal::BadHeaders { reason, .. } => {
                Some(reason)
            }
            RunRefusal::NothingToRerecord { why, .. } => Some(why),
            RunRefusal::BaselineStep { refusal } => refusal.reason(),
            _ => None,
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

/// `30000000` → `30,000,000`.
fn thousands(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}

/// `author.slug, author.id`, or what stands in for an empty list.
fn key_list(keys: &[String]) -> String {
    if keys.is_empty() {
        "(none)".to_string()
    } else {
        keys.join(", ")
    }
}

/// Why `--continue` is not a door, given the cursor's keys and the edited
/// file's (`None`: not read).
fn no_continue_reason(recorded: &[String], on_disk: Option<&[String]>) -> &'static str {
    match on_disk {
        _ if recorded.is_empty() => {
            "its cursor does not record the order keys it was committed under"
        }
        Some([]) => "the edited file's up is no longer @chunked",
        _ => "the edited file pages over different order keys than its cursor",
    }
}

impl std::fmt::Display for RunRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RunRefusal::EditedApplied {
                migration_name,
                migration,
                step,
                file,
                applied,
                applied_at,
                on_disk,
                down,
            } => {
                let on_disk = match on_disk {
                    Some(checksum) => format!("sha384:{checksum}"),
                    None => "missing".to_string(),
                };
                let nothing = if *down { "reverted" } else { "applied" };
                write!(
                    f,
                    "ferro migrate: {migration_name}/{file} was edited after it was applied to \
                     this database.\n  applied   sha384:{applied}  ({})\n  on disk   {on_disk}\n\
                     An applied step is never run again. Restore the file, or accept a \
                     deliberate edit with\n`ferro migrate rerecord {migration:04}:{step:02}`. \
                     Nothing was {nothing}.",
                    short_time(applied_at)
                )
            }
            RunRefusal::EditedChunked {
                migration_name,
                migration,
                step,
                file,
                rows_done,
                continue_allowed,
                keys_recorded,
                keys_on_disk,
                ..
            } => {
                let target = format!("{migration:04}:{step:02}");
                let continue_door = format!(
                    "  ferro migrate rerecord {target} --continue   keep them, continue from the \
                     cursor"
                );
                let restart_door = format!(
                    "  ferro migrate rerecord {target} --restart    run every row again from the \
                     start"
                );
                write!(
                    f,
                    "ferro migrate: {migration_name}/{file} was edited after {} rows were \
                     committed.\nFerro cannot tell whether those rows are right under the new \
                     code",
                    thousands(*rows_done)
                )?;
                match (continue_allowed, keys_on_disk) {
                    (true, Some(_)) => {
                        write!(f, ". Choose one:\n{continue_door}\n{restart_door}\n")?;
                    }
                    (true, None) => write!(
                        f,
                        ". Choose one:\n{continue_door}\n    (only while the edited file still \
                         pages over {})\n{restart_door}\n",
                        key_list(keys_recorded)
                    )?,
                    (false, _) => write!(
                        f,
                        ", and {}:\n  cursor    {}\n  on disk   {}\nso the cursor is no position \
                         in it. Restart from the first row:\n{restart_door}\n",
                        no_continue_reason(keys_recorded, keys_on_disk.as_deref()),
                        key_list(keys_recorded),
                        keys_on_disk
                            .as_deref()
                            .map_or_else(|| "not read".to_string(), key_list),
                    )?,
                }
                write!(f, "Nothing was applied.")
            }
            RunRefusal::EditedReverting {
                migration_name,
                migration,
                step,
                file,
                rows_done,
                keys_recorded,
                keys_on_disk,
            } => {
                let recorded = if keys_recorded.is_empty() {
                    "a cursor that does not record its order keys".to_string()
                } else {
                    format!("the order keys {}", key_list(keys_recorded))
                };
                let edited = if keys_on_disk.is_empty() {
                    "the edited down is no longer @chunked".to_string()
                } else {
                    format!("the edited down pages over {}", key_list(keys_on_disk))
                };
                write!(
                    f,
                    "ferro migrate: {migration_name}/{file} was edited while its down was \
                     part-way: {} rows were reverted under {recorded}, and {edited}, so its \
                     cursor is no position in it. Revert the remaining rows from the first:\n  \
                     ferro migrate rerecord {migration:04}:{step:02} --restart\nNothing was \
                     reverted.",
                    thousands(*rows_done)
                )
            }
            RunRefusal::RerecordTarget { target } => write!(
                f,
                "ferro migrate: rerecord re-records one step, named <migration>:<step> \
                 (0007:01), not `{target}`.\nA schema snapshot (ir.json) is never re-recorded: \
                 later migrations and historical models are built from it, so restore the \
                 file. Nothing was changed."
            ),
            RunRefusal::NothingToRerecord { target, why } => write!(
                f,
                "ferro migrate: nothing to re-record at {target}: {why}. Nothing was changed."
            ),
            RunRefusal::ModeNotApplicable {
                target,
                flag,
                state,
            } => write!(
                f,
                "ferro migrate: {flag} applies only to an unfinished chunked step with committed \
                 batches, and {target} is {state}.\nAccept its edit with `ferro migrate rerecord \
                 {target}`. Nothing was changed."
            ),
            RunRefusal::ContinueRefused {
                migration_name,
                target,
                file,
                keys_recorded,
                keys_on_disk,
            } => write!(
                f,
                "ferro migrate: cannot continue {migration_name}/{file} from its cursor: {}.\n  \
                 cursor    {}\n  on disk   {}\nThe cursor is no position in the edited query. \
                 Restart it with `ferro migrate rerecord {target} --restart`. Nothing was \
                 changed.",
                no_continue_reason(keys_recorded, Some(keys_on_disk)),
                key_list(keys_recorded),
                key_list(keys_on_disk)
            ),
            RunRefusal::SnapshotMismatch {
                migration_name,
                applied,
                on_disk,
            } => write!(
                f,
                "ferro migrate: {migration_name}/{SNAPSHOT_FILE} is not the snapshot this \
                 database applied {migration_name} under.\n  applied   sha384:{applied}\n  \
                 on disk   sha384:{on_disk}\nA snapshot is never edited: later migrations and \
                 historical models are built from it.\nRestore the file, or {}. Nothing was \
                 applied.",
                replaced_cause(migration_number(migration_name))
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
            RunRefusal::AppliedMissing {
                missing,
                directory,
                contract_steps,
                expand_only,
                replaced,
                ..
            } => {
                let verb = if missing.len() == 1 {
                    "which is"
                } else {
                    "which are"
                };
                write!(
                    f,
                    "ferro migrate: this database has applied {}, {verb} not in {directory}/.\n\
                     The directory is behind the database: check out the branch that holds it",
                    and_list(missing)
                )?;
                match replaced {
                    Some(number) => write!(f, ", or {}.", replaced_cause(Some(*number)))?,
                    None => write!(f, ".")?,
                }
                if let Some(first) = contract_steps.first().filter(|_| *expand_only) {
                    write!(
                        f,
                        "\nIt is ahead by contract steps, starting with {first} (contract: a \
                         write the previous release makes can fail beside it), so \
                         allow_ahead=\"expand-only\" does not let it through."
                    )?;
                }
                write!(f, " Nothing was applied.")
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
            RunRefusal::NoSuchMigration {
                migration,
                directory,
            } => write!(
                f,
                "ferro migrate: there is no migration {migration:04} in {directory}. Nothing was \
                 applied."
            ),
            RunRefusal::NotAMigration { target } => write!(
                f,
                "ferro migrate: through {target} is not a migration; write a migration number \
                 (0007). A step (0007:02) is not a target: no snapshot describes the state \
                 between two steps. Nothing was applied."
            ),
            RunRefusal::Unreadable { file, reason } => write!(
                f,
                "ferro migrate: cannot read {file} ({reason}). Nothing was applied."
            ),
            RunRefusal::Rebuild { file, reason } => {
                write!(f, "ferro migrate: {file} {reason}. Nothing was applied.")
            }
            RunRefusal::AlreadyTracked { applied, .. } => write!(
                f,
                "ferro migrate baseline: this database already has migration records ({}); \
                 baseline records migrations only on a database that has none. Run `ferro \
                 migrate status` to see where it stands. Nothing was recorded.",
                applied.join(", ")
            ),
            RunRefusal::AppliedAboveBaseline { above, floor } => {
                let (verb, pronoun) = if above.len() == 1 {
                    ("was", "it")
                } else {
                    ("were", "them")
                };
                write!(
                    f,
                    "ferro migrate baseline --remove: {} {verb} applied by a run above the \
                     baseline at {floor}. Revert {pronoun} first with `ferro migrate down --to \
                     {}`, then remove the baseline. Nothing was removed.",
                    above.join(", "),
                    number_of(floor)
                )
            }
            RunRefusal::NothingToBaseline { directory } => write!(
                f,
                "ferro migrate baseline: {directory}/ holds no migration to record; generate \
                 the first with `ferro migrate new <name>`. Nothing was recorded."
            ),
            RunRefusal::NoBaselineTarget {
                target,
                directory,
                head,
            } => write!(
                f,
                "ferro migrate baseline: {target} names no migration in {directory}/: give a \
                 migration number from 0001 to {}, or a migration's full name ({head}). \
                 Nothing was recorded.",
                number_of(head)
            ),
            RunRefusal::BaselineStep { refusal } => match refusal.as_ref() {
                RunRefusal::BadHeaders { file, reason } => write!(
                    f,
                    "ferro migrate baseline: {file}: {reason}. Nothing was recorded."
                ),
                missing @ RunRefusal::MissingRendering { .. } => {
                    write!(f, "ferro migrate baseline: {missing} Nothing was recorded.")
                }
                other => write!(f, "ferro migrate baseline: {other}"),
            },
            RunRefusal::Unclassifiable {
                migration_name,
                error,
                ..
            } => write!(
                f,
                "ferro migrate: {migration_name} does not regenerate from its snapshots, so \
                 ferro cannot decide the class of its generated steps (ADR-0054): {error}\nA \
                 step records the class the planner decides for it, never a guess. Restore \
                 {migration_name}/ir.json and the snapshot before it as `ferro migrate new` \
                 wrote them, or run the migration with the ferro version that generated it. \
                 No class was recorded for it, and none of its steps was applied."
            ),
            RunRefusal::PartialWitness {
                squash,
                parent,
                satisfied,
                missing,
            } => {
                let missing_list = and_list(missing);
                let applied = if satisfied.is_empty() {
                    format!("it has not wholly applied {missing_list}")
                } else {
                    format!("it applied {} but not {missing_list}", and_list(satisfied))
                };
                let them = if missing.len() == 1 { "it" } else { "them" };
                write!(
                    f,
                    "ferro migrate: {squash} replaces migrations this database applied only \
                     part of: {applied}, so it cannot adopt {squash}. To keep its rows, check \
                     out the commit that holds {missing_list}, run `ferro migrate up` there to \
                     finish {them}, then run `ferro migrate up` here, which adopts {squash}. Or \
                     revert instead: on the commit that applied them run `ferro migrate down \
                     --to {}`, then `ferro migrate up` here; the revert drops what they created \
                     and fails on an irreversible step. Nothing was changed.",
                    number_of(parent)
                )
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

/// One read of the migrations directory for a run (ADR-0028, ADR-0048): the
/// verified directory and the raw bytes of every SQL step file it holds,
/// each the bytes its checksum was taken over. A run plans from it and
/// executes from it, and never goes back to disk: the bytes hashed are the
/// bytes planned and the bytes run.
#[derive(Clone, Debug, PartialEq)]
pub struct HeldDirectory {
    /// The directory, read and verified.
    pub dir: MigrationsDir,
    /// Every SQL step file's bytes, by path.
    sql: BTreeMap<PathBuf, Arc<[u8]>>,
}

impl HeldDirectory {
    /// Read `path` for a run: [`read_for_run`], then every SQL step file's
    /// bytes, each checked against the checksum the directory read took.
    ///
    /// # Errors
    /// [`read_for_run`]'s refusals; [`RunRefusal::Unreadable`] for a file
    /// that cannot be read again, or whose bytes changed in between.
    pub fn read(path: &Path) -> Result<Self, RunRefusal> {
        let dir = read_for_run(path)?;
        let mut sql = Vec::new();
        for (file, _) in sql_files(&dir) {
            let bytes = std::fs::read(file).map_err(|err| RunRefusal::Unreadable {
                file: shown_path(&dir, file),
                reason: err.to_string(),
            })?;
            sql.push((file.clone(), bytes));
        }
        Self::from_parts(dir, sql)
    }

    /// `dir` with the given SQL file bytes, each checked against the
    /// checksum `dir` holds for its path.
    ///
    /// # Errors
    /// [`RunRefusal::Unreadable`] for bytes that are not the hashed ones, or
    /// a path `dir` holds no SQL step file at.
    pub fn from_parts(
        dir: MigrationsDir,
        files: impl IntoIterator<Item = (PathBuf, Vec<u8>)>,
    ) -> Result<Self, RunRefusal> {
        let checksums: BTreeMap<&PathBuf, &[u8; 48]> = sql_files(&dir).collect();
        let mut sql = BTreeMap::new();
        for (path, bytes) in files {
            let reason = match checksums.get(&path) {
                None => Some("the migrations directory holds no step file there"),
                Some(checksum) if sha384(&bytes) != **checksum => Some(
                    "it changed while the migrations directory was read; run the command again",
                ),
                Some(_) => None,
            };
            if let Some(reason) = reason {
                return Err(RunRefusal::Unreadable {
                    file: shown_path(&dir, &path),
                    reason: reason.to_string(),
                });
            }
            sql.insert(path, Arc::from(bytes));
        }
        Ok(Self { dir, sql })
    }

    /// The held bytes of the SQL step file at `path`.
    pub fn bytes(&self, path: &Path) -> Option<&Arc<[u8]>> {
        self.sql.get(path)
    }

    /// The held text of the SQL step file at `path`.
    ///
    /// # Errors
    /// [`RunRefusal::Unreadable`] when no bytes are held for it, or they are
    /// not UTF-8.
    pub fn text(&self, path: &Path) -> Result<&str, RunRefusal> {
        let unreadable = |reason: &str| RunRefusal::Unreadable {
            file: shown_path(&self.dir, path),
            reason: reason.to_string(),
        };
        let bytes = self
            .bytes(path)
            .ok_or_else(|| unreadable("it was not read with the migrations directory"))?;
        std::str::from_utf8(bytes).map_err(|err| unreadable(&format!("not UTF-8 text: {err}")))
    }

    /// Plan a run from the held read: [`plan_run`], then what only the
    /// steps' own bytes say. Every SQL step that runs statements must read
    /// as text, and each SQLite `foreign-keys-off` step carries the tables
    /// its file rebuilds ([`PlannedStep::rebuilds`]), lexed once here.
    ///
    /// # Errors
    /// [`plan_run`]'s refusals; [`RunRefusal::Unreadable`] for a SQL step
    /// that is not UTF-8; [`RunRefusal::Rebuild`] for a rebuild the
    /// snapshots cannot describe.
    pub fn plan(
        &self,
        records: &[StepRecord],
        dialect: Dialect,
        direction: Direction,
        allow_ahead: impl Into<AllowAhead>,
        order_keys: Option<&OrderKeys>,
    ) -> Result<RunPlan, RunRefusal> {
        let mut plan = plan_run(
            &self.dir,
            records,
            dialect,
            direction,
            allow_ahead,
            order_keys,
        )?;
        let down = matches!(direction, Direction::Down { .. });
        for step in &mut plan.steps {
            if step.data || (down && step.nothing_to_reverse.is_some()) {
                continue;
            }
            let text = self.text(&step.path)?;
            if step.mode == ExecMode::ForeignKeysOff {
                step.rebuilds = rebuild_expectations(&self.dir, step, text, dialect, down)?;
            }
        }
        Ok(plan)
    }
}

/// Every SQL step file of `dir` (up and down, every dialect's rendering)
/// with the checksum the directory read took of it.
fn sql_files(dir: &MigrationsDir) -> impl Iterator<Item = (&PathBuf, &[u8; 48])> {
    dir.migrations
        .iter()
        .flat_map(|migration| &migration.steps)
        .filter(|step| step.kind != StepKind::Data)
        .flat_map(|step| step.files.values())
        .flat_map(|file| {
            std::iter::once((&file.up, &file.up_checksum))
                .chain(file.down.as_ref().zip(file.down_checksum.as_ref()))
        })
}

/// `NNNN_<name>/<file>` for a step file of `dir`, else the path itself.
fn shown_path(dir: &MigrationsDir, path: &Path) -> String {
    dir.migrations
        .iter()
        .find(|migration| path.parent() == Some(migration.dir.as_path()))
        .map_or_else(
            || path.display().to_string(),
            |migration| format!("{}/{}", migration.dir_name(), file_name(path)),
        )
}

/// The tables `step` (a SQLite `foreign-keys-off` step whose file is
/// `text`) rebuilds, in the file's order, each with its starting name and
/// declared columns ([`RebuildExpectation`]). Going up the file starts from
/// the parent's snapshot and the migration's own is the other side; going
/// down the other way round, with the migration's rename hints reversed.
///
/// # Errors
/// [`RunRefusal::Rebuild`] for a rebuilt table the starting snapshot does
/// not declare, or a snapshot carrying a rename hint the generator refuses.
fn rebuild_expectations(
    dir: &MigrationsDir,
    step: &PlannedStep,
    text: &str,
    dialect: Dialect,
    down: bool,
) -> Result<Vec<RebuildExpectation>, RunRefusal> {
    let tables = rebuilt_tables(&split_statements(text, dialect));
    if tables.is_empty() {
        return Ok(Vec::new());
    }
    let shown = format!("{}/{}", step.migration_name, step.file);
    let refused = |reason: String| RunRefusal::Rebuild {
        file: shown.clone(),
        reason,
    };
    let Some(migration) = dir.migrations.iter().find(|m| m.number == step.migration) else {
        return Err(refused(
            "belongs to no migration of the directory".to_string(),
        ));
    };
    let own = &migration.snapshot.ir.payload;
    let parent = parent_of(dir, migration).map(|m| &m.snapshot.ir.payload);
    let empty = SchemaIrPayload {
        dialect_agnostic: own.dialect_agnostic,
        models: Vec::new(),
    };
    let hints = live_hints(parent.unwrap_or(&empty), own).map_err(|err| {
        refused(format!(
            "cannot be checked: its schema snapshot carries a rename hint ferro refuses: {err}"
        ))
    })?;
    let (starts, other, hints) = if down {
        (Some(own), parent, reverse_hints(&hints))
    } else {
        (parent, Some(own), hints)
    };
    let mut out = Vec::new();
    for table in tables {
        let starting_name = hints
            .iter()
            .find_map(|hint| match hint {
                Hint::Table { old, new } if *new == table => Some(old.clone()),
                _ => None,
            })
            .unwrap_or_else(|| table.clone());
        let mut declared = starts
            .and_then(|ir| ir.models.iter().find(|m| m.table_name == starting_name))
            .cloned()
            .ok_or_else(|| {
                refused(format!(
                    "rebuilds table \"{}\", which the schema snapshot it starts from does not \
                     declare",
                    table.replace('"', "\"\"")
                ))
            })?;
        if let Some(model) = other.and_then(|ir| ir.models.iter().find(|m| m.table_name == table)) {
            for column in &model.columns {
                if !declared
                    .columns
                    .iter()
                    .any(|known| known.name == column.name)
                {
                    declared.columns.push(column.clone());
                }
            }
        }
        out.push(RebuildExpectation {
            table,
            starting_name,
            declared,
        });
    }
    Ok(out)
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

/// The `NNNN` of `NNNN_<name>`.
fn migration_number(migration_name: &str) -> Option<u16> {
    migration_name.get(..4)?.parse().ok()
}

/// The second cause of a snapshot mismatch or an applied migration the
/// directory lacks, and its fix: the migration was replaced (`ferro migrate
/// new --replace`) after this database applied it, so it is reverted with
/// the files that were applied before the new ones go up.
fn replaced_cause(number: Option<u16>) -> String {
    let (number, parent) = match number {
        Some(number) => (
            format!("{number:04}"),
            format!("{:04}", number.saturating_sub(1)),
        ),
        None => ("the migration".to_string(), "<parent>".to_string()),
    };
    format!(
        "{number} was replaced: revert it with the files that were applied (check out the \
         commit that applied it, `ferro migrate down --to {parent}`), then `up`"
    )
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
/// for. Returns the ahead migrations `allow_ahead` let through: any of them
/// with [`AllowAhead::Any`], only when every ahead step record is `expand`
/// with [`AllowAhead::ExpandOnly`] (a record's class read from the record
/// alone: the directory lacks the files).
fn check_records(
    dir: &MigrationsDir,
    records: &[StepRecord],
    allow_ahead: AllowAhead,
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
    let mut contract_steps = Vec::new();
    let mut replaced: Option<u16> = None;
    let mut lacks = |number: u16| replaced = Some(replaced.map_or(number, |r| r.min(number)));
    for record in records {
        match dir
            .migrations
            .get(usize::from(record.migration).wrapping_sub(1))
        {
            None if record.migration > head => {
                ahead.insert(record.migration_name.clone());
                if record.class_or_contract() != StepClass::Expand {
                    contract_steps.push(record.address());
                }
            }
            None => {
                missing.insert(record.migration_name.clone());
                lacks(record.migration);
            }
            Some(migration) if migration.dir_name() != record.migration_name => {
                missing.insert(record.migration_name.clone());
                lacks(record.migration);
            }
            Some(migration) if !migration.steps.iter().any(|s| s.ordinal == record.step) => {
                missing.insert(record.path());
                lacks(record.migration);
            }
            Some(_) => {}
        }
    }
    let ahead_allowed = match allow_ahead {
        AllowAhead::No => ahead.is_empty(),
        AllowAhead::ExpandOnly => contract_steps.is_empty(),
        AllowAhead::Any => true,
    };
    if !missing.is_empty() || !ahead_allowed {
        // Whether `allow_ahead` alone would let the run through is the
        // caller's to answer: only it knows what else the run meets.
        return Err(RunRefusal::AppliedMissing {
            missing: ahead.iter().chain(missing.iter()).cloned().collect(),
            directory: directory_label(dir),
            ahead_only: false,
            contract_steps,
            expand_only: allow_ahead == AllowAhead::ExpandOnly,
            replaced,
        });
    }
    Ok(ahead.into_iter().collect())
}

/// Every applied step and snapshot against the directory: a snapshot whose
/// checksum is not the recorded one, a finished step whose file changed. A
/// `reverting` record is an unfinished attempt (its `down` stopped
/// part-way, ADR-0030): its edit is the caller's to judge
/// ([`edited_reverting`]). `down` says which run met the refusal.
fn check_applied(
    dir: &MigrationsDir,
    by_key: &RecordMap,
    dialect: Dialect,
    down: bool,
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
            if !record.is_finished() || record.reverting {
                continue;
            }
            let file = step_file(migration, step, dialect)?;
            let checksum = encode_checksum(&file.up_checksum);
            if record.checksum != checksum || record.file != file_name(&file.up) {
                return Err(RunRefusal::EditedApplied {
                    migration_name: migration.dir_name(),
                    migration: migration.number,
                    step: step.ordinal,
                    file: record.file.clone(),
                    applied: record.checksum.clone(),
                    applied_at: record.finished_at.clone().unwrap_or_default(),
                    on_disk: (record.file == file_name(&file.up)).then_some(checksum),
                    down,
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

/// The order keys each chunked data step's `up` pages over, read from its
/// file on disk (a fact only Python can read: the query is a lambda over the
/// migration's historical models). Each key is `<table>.<column>`, with
/// ` desc` for a descending one (`author.id`, `author.created_at desc`); an
/// empty list says the file's `up` is not `@chunked`.
pub type OrderKeys = BTreeMap<(u16, u8), Vec<String>>;

/// The order keys a chunked cursor was committed under: its `order_by`
/// member (`{"keys": [...], "order_by": ["author.id"], "rows_done": N}`);
/// empty for a cursor that does not record them.
pub fn cursor_order_keys(cursor: &str) -> Vec<String> {
    serde_json::from_str::<serde_json::Value>(cursor)
        .ok()
        .and_then(|value| {
            value.get("order_by")?.as_array().map(|keys| {
                keys.iter()
                    .filter_map(|k| k.as_str().map(str::to_string))
                    .collect()
            })
        })
        .unwrap_or_default()
}

/// Whether `record` is an unfinished chunked step with committed batches
/// (ADR-0030): its edit is refused, not accepted.
fn has_committed_batches(record: &StepRecord) -> bool {
    !record.is_finished() && record.kind == RecordKind::Chunked && record.resume_cursor.is_some()
}

/// The edited-chunked refusal for `record`, whose step's file on disk is
/// `file` with checksum `on_disk`; `order_keys` (when read) decides whether
/// continuing is a door.
fn edited_chunked(
    record: &StepRecord,
    file: String,
    on_disk: String,
    order_keys: Option<&OrderKeys>,
) -> RunRefusal {
    let keys_recorded = record
        .resume_cursor
        .as_deref()
        .map(cursor_order_keys)
        .unwrap_or_default();
    let keys_on_disk = order_keys.map(|keys| {
        keys.get(&(record.migration, record.step))
            .cloned()
            .unwrap_or_default()
    });
    let continue_allowed = !keys_recorded.is_empty()
        && keys_on_disk
            .as_ref()
            .is_none_or(|keys| *keys == keys_recorded);
    RunRefusal::EditedChunked {
        migration_name: record.migration_name.clone(),
        migration: record.migration,
        step: record.step,
        file,
        recorded: record.checksum.clone(),
        on_disk,
        rows_done: record.rows_done.unwrap_or(0),
        continue_allowed,
        keys_recorded,
        keys_on_disk,
    }
}

/// The judgement on an edited `reverting` record (ADR-0030): an unfinished
/// attempt, so its edit is accepted on `--continue`'s condition, which the
/// down's own cursor sets: its `revert_cursor` must record the order keys
/// it was committed under, and the edited file's `down` (`order_keys`, when
/// read; [`OrderKeys`] carries a reverting step's `down` keys) must page
/// over the same ones. A record whose cursor `rerecord --restart` cleared
/// holds no position, so any edit is accepted. `None` accepts; the refusal
/// names `rerecord --restart`.
fn edited_reverting(
    record: &StepRecord,
    file: &str,
    order_keys: Option<&OrderKeys>,
) -> Option<RunRefusal> {
    let cursor = record.revert_cursor.as_deref()?;
    let keys_recorded = cursor_order_keys(cursor);
    let keys_on_disk = order_keys.map(|keys| {
        keys.get(&(record.migration, record.step))
            .cloned()
            .unwrap_or_default()
    });
    let continues = !keys_recorded.is_empty()
        && keys_on_disk
            .as_ref()
            .is_none_or(|keys| *keys == keys_recorded);
    if continues {
        return None;
    }
    Some(RunRefusal::EditedReverting {
        migration_name: record.migration_name.clone(),
        migration: record.migration,
        step: record.step,
        file: file.to_string(),
        rows_done: record.rows_done.unwrap_or(0),
        keys_recorded,
        keys_on_disk: keys_on_disk.unwrap_or_default(),
    })
}

/// Plan a run: every pending step of every pending migration, in order, or
/// the refusal that stops it before anything runs.
///
/// A step is pending when it has no finished record. A started-but-unfinished
/// record resumes at that step; if its file changed since, the edit is
/// accepted and re-recorded (ADR-0030), and [`PlannedStep::edited`] says so —
/// except an unfinished chunked step with committed batches, which is
/// refused ([`RunRefusal::EditedChunked`]). `order_keys` are the edited
/// files' order keys when the caller read them ([`OrderKeys`]); they decide
/// whether that refusal offers `--continue` outright (`None`: it is offered
/// on the condition that the keys did not change).
///
/// # Errors
/// A [`RunRefusal`]: a reverting record; records for migrations the directory
/// lacks (allowed through with `allow_ahead` when they all sort above its
/// head); a snapshot or finished step edited after it was applied; an
/// edited chunked step with committed batches; a pending migration below an
/// applied one; a DDL step without this dialect's rendering; headers the
/// dialect cannot honour; a `through` the directory does not hold
/// ([`RunRefusal::NoSuchMigration`]). Records ahead of the directory refused
/// without `allow_ahead` say whether it alone would have let the run
/// through ([`RunRefusal::ahead_only`]). Going down, see [`plan_down`]'s
/// refusals.
pub fn plan_run(
    dir: &MigrationsDir,
    records: &[StepRecord],
    dialect: Dialect,
    direction: Direction,
    allow_ahead: impl Into<AllowAhead>,
    order_keys: Option<&OrderKeys>,
) -> Result<RunPlan, RunRefusal> {
    let allow_ahead = allow_ahead.into();
    let through = match direction {
        Direction::Down { target } => {
            return plan_down(dir, records, dialect, target, order_keys);
        }
        Direction::Up { through } => through,
    };
    match plan_up(dir, records, dialect, through, allow_ahead, order_keys) {
        Err(RunRefusal::AppliedMissing {
            missing,
            directory,
            contract_steps,
            expand_only,
            replaced,
            ..
        }) if allow_ahead != AllowAhead::Any => Err(RunRefusal::AppliedMissing {
            missing,
            directory,
            ahead_only: plan_up(dir, records, dialect, through, AllowAhead::Any, order_keys)
                .is_ok(),
            contract_steps,
            expand_only,
            replaced,
        }),
        planned => planned,
    }
}

/// [`plan_run`] going up.
fn plan_up(
    dir: &MigrationsDir,
    records: &[StepRecord],
    dialect: Dialect,
    through: Option<u16>,
    allow_ahead: AllowAhead,
    order_keys: Option<&OrderKeys>,
) -> Result<RunPlan, RunRefusal> {
    let ahead = check_records(dir, records, allow_ahead, true)?;
    let by_key: RecordMap = records.iter().map(|r| ((r.migration, r.step), r)).collect();
    check_applied(dir, &by_key, dialect, false)?;
    check_order(dir, &by_key)?;
    if let Some(number) = through
        && !dir.migrations.iter().any(|m| m.number == number)
    {
        return Err(RunRefusal::NoSuchMigration {
            migration: number,
            directory: dir.path.display().to_string(),
        });
    }

    let mut steps = Vec::new();
    for migration in &dir.migrations {
        if through.is_some_and(|last| migration.number > last) {
            break;
        }
        let snapshot_checksum = encode_checksum(&migration.snapshot.checksum);
        let first_data_step = first_data_step(migration);
        // Decided once per migration with a pending step (ADR-0054).
        let mut classes: Option<BTreeMap<u8, StepClass>> = None;
        for step in &migration.steps {
            let record = by_key.get(&(migration.number, step.ordinal));
            if record.is_some_and(|r| r.is_finished()) {
                continue;
            }
            if classes.is_none() {
                classes = Some(migration_classes(dir, migration, dialect)?);
            }
            let class = classes
                .as_ref()
                .and_then(|classes| classes.get(&step.ordinal))
                .copied()
                .unwrap_or(StepClass::Contract);
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
            let edited_record = record.filter(|r| r.checksum != checksum || r.file != name);
            if let Some(r) = edited_record
                && has_committed_batches(r)
            {
                return Err(edited_chunked(r, name, checksum, order_keys));
            }
            let edited = edited_record.map(|r| EditedUnfinished {
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
                    class: Some(class),
                },
                first_data_step,
                rebuilds: Vec::new(),
            });
        }
    }
    Ok(RunPlan { steps, ahead })
}

/// The ordinal of `migration`'s first data step, when it has one.
fn first_data_step(migration: &Migration) -> Option<u8> {
    migration
        .steps
        .iter()
        .filter(|step| step.kind == StepKind::Data)
        .map(|step| step.ordinal)
        .min()
}

/// The migration before `migration` in `dir`.
fn parent_of<'a>(dir: &'a MigrationsDir, migration: &Migration) -> Option<&'a Migration> {
    let number = migration.number.checked_sub(1).filter(|n| *n > 0)?;
    dir.migrations.iter().find(|m| m.number == number)
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
/// A chunked step whose down stopped part-way (its record `reverting`) is
/// an unfinished attempt (ADR-0030): its edited file is accepted, re-recorded
/// and [`PlannedStep::edited`] says so, when its down still pages over the
/// order keys its revert cursor was committed under (`order_keys`, the
/// edited files' `down` keys; `None` accepts and leaves the check to the
/// run's own cursor decode).
///
/// # Errors
/// Before anything is reverted: a record for a migration or step the
/// directory lacks (the down files come only from disk, so a database ahead
/// of the checkout is refused whatever `allow_ahead` says); a snapshot or
/// finished step edited since it was applied; a reverting step edited so
/// its down pages over other order keys ([`RunRefusal::EditedReverting`],
/// naming `rerecord --restart`); a `--to` naming nothing;
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
    order_keys: Option<&OrderKeys>,
) -> Result<RunPlan, RunRefusal> {
    check_records(dir, records, AllowAhead::No, false)?;
    let by_key: RecordMap = records.iter().map(|r| ((r.migration, r.step), r)).collect();
    check_applied(dir, &by_key, dialect, true)?;
    let floor = down_floor(dir, records, target)?;
    let baseline_floor = records
        .iter()
        .filter(|r| r.origin.is_baseline())
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
        // A reverting step's file is one file for its up and its down: its
        // edit is judged against the record's checksum, and once accepted
        // the record takes the new checksum with its next write.
        let mut standing = (*record).clone();
        let mut edited = None;
        let up_name = file_name(&file.up);
        let up_checksum = encode_checksum(&file.up_checksum);
        if record.reverting && (record.checksum != up_checksum || record.file != up_name) {
            if let Some(refusal) = edited_reverting(record, &up_name, order_keys) {
                return Err(refusal);
            }
            edited = Some(EditedUnfinished {
                recorded: record.checksum.clone(),
                recorded_file: record.file.clone(),
            });
            standing.checksum = up_checksum;
            standing.file = up_name;
        }
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
            edited,
            nothing_to_reverse,
            data,
            record: standing,
            first_data_step: first_data_step(migration),
            rebuilds: Vec::new(),
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

// -- adoption (ADR-0056) -------------------------------------------------------

/// A squash a run adopts: the database applied every migration it replaced,
/// as its witness records them, so the run swaps their records for the
/// squash's and runs nothing (ADR-0056).
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Adoption {
    /// `NNNN_<name>` of the squash.
    pub squash: String,
    /// `NNNN_<name>` of every migration whose records witness it, in chain
    /// order: what the database had applied (the earliest originals of a
    /// squash of a squash, when it still holds those).
    pub replaced: Vec<String>,
    /// `(migration, step)` of every witnessed record, ascending: the records
    /// adoption deletes.
    pub witnessed: Vec<(u16, u8)>,
}

/// A squash whose witness the tracking table holds only part of: some of
/// the migrations it replaced are applied whole, the rest are not (absent,
/// short of a step, failed, or under another snapshot). Never adopted; a run
/// refuses on it ([`RunRefusal::PartialWitness`]).
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct PartialWitness {
    /// `NNNN_<name>` of the squash.
    pub squash: String,
    /// `NNNN_<name>` of the migration below it, `0000` when it is the first.
    pub parent: String,
    /// The replaced migrations the records hold whole, in chain order.
    pub satisfied: Vec<String>,
    /// The ones they do not, in chain order.
    pub missing: Vec<String>,
}

/// The records by migration name, each name's in step order.
type ByName<'a> = BTreeMap<&'a str, Vec<&'a StepRecord>>;

fn by_name(records: &[StepRecord]) -> ByName<'_> {
    let mut by_name: ByName<'_> = BTreeMap::new();
    for record in records {
        by_name
            .entry(record.migration_name.as_str())
            .or_default()
            .push(record);
    }
    for records in by_name.values_mut() {
        records.sort_by_key(|r| (r.migration, r.step));
    }
    by_name
}

/// Whether a record stands for a step applied whole: finished, not part-way
/// down, and carrying no failed down's error.
fn applied_whole(record: &StepRecord) -> bool {
    record.is_finished() && !record.reverting && record.error.is_none()
}

/// The records that witness `entry`: its own (exactly one finished record
/// per recorded ordinal, each under the recorded snapshot checksum, and no
/// other record of its name), or, for a replaced squash with no record of
/// its own, every nested entry's. `None` when the records do not witness it.
fn witnessing<'a>(entry: &WitnessEntry, records: &ByName<'a>) -> Option<Vec<&'a StepRecord>> {
    if let Some(own) = records.get(entry.migration.as_str()) {
        let snapshot = encode_checksum(&entry.snapshot_checksum);
        let ordinals: Vec<u8> = own.iter().map(|r| r.step).collect();
        let whole = ordinals == entry.steps
            && own
                .iter()
                .all(|r| applied_whole(r) && r.snapshot_checksum == snapshot);
        return whole.then(|| own.clone());
    }
    if entry.replaces.is_empty() {
        return None;
    }
    let mut all = Vec::new();
    for nested in &entry.replaces {
        all.extend(witnessing(nested, records)?);
    }
    Some(all)
}

/// Whether any record names `entry` or a migration it nests.
fn touched(entry: &WitnessEntry, records: &ByName<'_>) -> bool {
    records.contains_key(entry.migration.as_str())
        || entry.replaces.iter().any(|nested| touched(nested, records))
}

/// `NNNN_<name>` of every migration in `witnessed`, once each, in chain
/// order: records sort by number, and two migrations of one number (an
/// original and a later one numbered after a squash) by where the witness
/// names them, which the order of `witnessed` already is.
fn names_of(witnessed: &[&StepRecord]) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for record in witnessed {
        if !names.contains(&record.migration_name) {
            names.push(record.migration_name.clone());
        }
    }
    names
}

/// Every squash in `dir` whose witness `records` wholly satisfy (ADR-0056):
/// for every entry, finished records for exactly the recorded ordinals,
/// each under the recorded snapshot checksum, and no other record of that
/// migration's name; or, recursively, every entry of its nested list
/// satisfied. Baseline records count, and so do records whose up-file
/// checksum differs from the file: the snapshot checksum is the proof
/// (ADR-0030). The tracking table is the whole witness; nothing live is
/// read.
pub fn adoptable(dir: &MigrationsDir, records: &[StepRecord]) -> Vec<Adoption> {
    let records = by_name(records);
    dir.migrations
        .iter()
        .filter(|m| !m.witness.is_empty())
        .filter_map(|squash| {
            let mut witnessed = Vec::new();
            for entry in &squash.witness {
                witnessed.extend(witnessing(entry, &records)?);
            }
            let replaced = names_of(&witnessed);
            let mut keys: Vec<(u16, u8)> =
                witnessed.iter().map(|r| (r.migration, r.step)).collect();
            keys.sort_unstable();
            Some(Adoption {
                squash: squash.dir_name(),
                replaced,
                witnessed: keys,
            })
        })
        .collect()
}

/// Sort `entries` into what `records` hold whole and what they do not,
/// descending into a replaced squash the records name only through its
/// originals, so the names are the migrations the database applied.
fn sort_witness(
    entries: &[WitnessEntry],
    records: &ByName<'_>,
    satisfied: &mut Vec<String>,
    missing: &mut Vec<String>,
) {
    for entry in entries {
        if let Some(witnessed) = witnessing(entry, records) {
            satisfied.extend(names_of(&witnessed));
        } else if !records.contains_key(entry.migration.as_str())
            && entry.replaces.iter().any(|nested| touched(nested, records))
        {
            sort_witness(&entry.replaces, records, satisfied, missing);
        } else {
            missing.push(entry.migration.clone());
        }
    }
}

/// Every squash in `dir` whose witness `records` hold part of: a record
/// names a migration it replaced, yet the witness is not wholly satisfied
/// ([`adoptable`]). A squash none of whose replaced migrations has a record
/// is not one: that database never ran the originals.
pub fn partial_witnesses(dir: &MigrationsDir, records: &[StepRecord]) -> Vec<PartialWitness> {
    let by = by_name(records);
    let adopted: BTreeSet<String> = adoptable(dir, records)
        .into_iter()
        .map(|a| a.squash)
        .collect();
    dir.migrations
        .iter()
        .filter(|m| !m.witness.is_empty() && !adopted.contains(&m.dir_name()))
        .filter(|m| m.witness.iter().any(|entry| touched(entry, &by)))
        .map(|squash| {
            let (mut satisfied, mut missing) = (Vec::new(), Vec::new());
            sort_witness(&squash.witness, &by, &mut satisfied, &mut missing);
            PartialWitness {
                squash: squash.dir_name(),
                parent: parent_of(dir, squash)
                    .map_or_else(|| "0000".to_string(), Migration::dir_name),
                satisfied,
                missing,
            }
        })
        .collect()
}

/// Each data step of every squash a run would adopt, `(migration, step,
/// file)`: the files whose `up` declaration (atomic or chunked) the caller
/// reads for its record's kind, as a baseline does, since adoption runs
/// none of them.
pub fn adoption_data_steps(dir: &MigrationsDir, records: &[StepRecord]) -> Vec<(u16, u8, PathBuf)> {
    let adopted: BTreeSet<String> = adoptable(dir, records)
        .into_iter()
        .map(|a| a.squash)
        .collect();
    dir.migrations
        .iter()
        .filter(|m| adopted.contains(&m.dir_name()))
        .flat_map(|m| {
            m.steps
                .iter()
                .filter(|s| s.kind == StepKind::Data)
                .filter_map(move |s| {
                    s.files
                        .get(&StepDialect::Portable)
                        .map(|file| (m.number, s.ordinal, file.up.clone()))
                })
        })
        .collect()
}

/// What a run's adoption does ([`adoption`]): the squashes it adopts, the
/// records it deletes and inserts, and the records the database holds after.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct AdoptionPlan {
    /// The squashes adopted, in directory order.
    pub adoptions: Vec<Adoption>,
    /// `(migration, step)` of every witnessed record, deleted.
    pub removed: Vec<(u16, u8)>,
    /// One finished record per step of every adopted squash, inserted.
    pub inserted: Vec<StepRecord>,
    /// The records after the swap, by migration and step: what the run
    /// plans against.
    pub records: Vec<StepRecord>,
}

/// The adoption a run makes on a `dialect` database whose tracking table
/// holds `records` (ADR-0056): each [`adoptable`] squash's witnessed records
/// deleted, and one finished record inserted per step of the squash, with
/// this dialect's up file name and checksum, the squash's snapshot checksum,
/// the class the step-class entry point decides (ADR-0054), `now` as
/// started and finished, `ferro_version`, origin `adopted`, and for a data
/// step the kind `kinds` holds for it (its `up`'s declaration, read by the
/// caller from [`adoption_data_steps`]; `atomic` when absent).
///
/// # Errors
/// [`RunRefusal::PartialWitness`] for the first squash whose witness the
/// records hold part of, before anything is adopted; a squash step with no
/// rendering for `dialect` or headers it cannot honour; a squash whose
/// generated steps get no class ([`RunRefusal::Unclassifiable`]).
pub fn adoption(
    dir: &MigrationsDir,
    records: &[StepRecord],
    dialect: Dialect,
    kinds: &BTreeMap<(u16, u8), RecordKind>,
    now: &str,
    ferro_version: &str,
) -> Result<AdoptionPlan, RunRefusal> {
    if let Some(partial) = partial_witnesses(dir, records).into_iter().next() {
        return Err(RunRefusal::PartialWitness {
            squash: partial.squash,
            parent: partial.parent,
            satisfied: partial.satisfied,
            missing: partial.missing,
        });
    }
    let adoptions = adoptable(dir, records);
    let mut plan = AdoptionPlan {
        records: records.to_vec(),
        ..AdoptionPlan::default()
    };
    for adoption in &adoptions {
        let Some(squash) = dir
            .migrations
            .iter()
            .find(|m| m.dir_name() == adoption.squash)
        else {
            continue;
        };
        let snapshot_checksum = encode_checksum(&squash.snapshot.checksum);
        let classes = migration_classes(dir, squash, dialect)?;
        for step in &squash.steps {
            let file = step_file(squash, step, dialect)?;
            let name = file_name(&file.up);
            let kind = if step.kind == StepKind::Data {
                kinds
                    .get(&(squash.number, step.ordinal))
                    .copied()
                    .unwrap_or(RecordKind::Atomic)
            } else {
                exec_mode(
                    &file.headers,
                    dialect,
                    &format!("{}/{name}", squash.dir_name()),
                )?
                .record_kind()
            };
            plan.inserted.push(StepRecord {
                migration: squash.number,
                step: step.ordinal,
                migration_name: squash.dir_name(),
                file: name,
                kind,
                checksum: encode_checksum(&file.up_checksum),
                snapshot_checksum: snapshot_checksum.clone(),
                started_at: now.to_string(),
                finished_at: Some(now.to_string()),
                failed_at: None,
                error: None,
                resume_cursor: None,
                rows_done: None,
                duration_ms: 0,
                ferro_version: ferro_version.to_string(),
                origin: Origin::Adopted,
                reverting: false,
                revert_cursor: None,
                class: Some(
                    classes
                        .get(&step.ordinal)
                        .copied()
                        .unwrap_or(StepClass::Contract),
                ),
            });
        }
        plan.removed.extend(adoption.witnessed.iter().copied());
    }
    plan.removed.sort_unstable();
    plan.records
        .retain(|r| plan.removed.binary_search(&(r.migration, r.step)).is_err());
    plan.records.extend(plan.inserted.iter().cloned());
    plan.records.sort_by_key(|r| (r.migration, r.step));
    plan.adoptions = adoptions;
    Ok(plan)
}

/// What `ferro migrate rerecord` is asked to do (#473).
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RerecordMode {
    /// Accept the edited file: the record's checksum (and file and kind)
    /// change, nothing else.
    Record,
    /// `--continue`: an edited chunked step with committed batches keeps
    /// them and resumes from its cursor.
    Continue,
    /// `--restart`: an edited chunked step with committed batches clears its
    /// cursor and `rows_done`, so `up` starts from the first row.
    Restart,
}

impl RerecordMode {
    fn flag(self) -> &'static str {
        match self {
            RerecordMode::Record => "",
            RerecordMode::Continue => "--continue",
            RerecordMode::Restart => "--restart",
        }
    }
}

/// The record rewrite `rerecord` performs: one step record's `file`,
/// `checksum` and `kind`, and with `clear_cursor` its `resume_cursor` and
/// `rows_done`. It runs no SQL of the step.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct RerecordAction {
    /// `NNNN`.
    pub migration: u16,
    /// `NN`.
    pub step: u8,
    /// `NNNN_<name>`.
    pub migration_name: String,
    /// The file the record names now.
    pub recorded_file: String,
    /// The file on disk, which the record will name.
    pub file: String,
    /// That file's path (Python reads a data step's declared kind from it).
    pub path: PathBuf,
    /// The checksum the record holds now.
    pub old_checksum: String,
    /// The on-disk file's checksum, which the record will hold.
    pub new_checksum: String,
    /// The kind the record will hold: a SQL step's from its headers; a data
    /// step's as recorded until Python reads its declaration (`data`).
    pub kind: RecordKind,
    /// A Python data step: its kind is its file's `up` declaration.
    pub data: bool,
    /// Whether the record finished.
    pub finished: bool,
    /// `--restart`: clear the cursor (a reverting record's `revert_cursor`,
    /// any other's `resume_cursor`) and set `rows_done` to 0.
    pub clear_cursor: bool,
    /// The record is `reverting`: its down stopped part-way, and `--restart`
    /// clears its `revert_cursor` (it stays reverting, so the next `down`
    /// walks the rows left from the first).
    #[serde(default)]
    pub reverting: bool,
}

/// `0007:01` → `(7, 1)`; anything else (`0007`, `0007:ir`) is no step.
fn parse_step_target(target: &str) -> Option<(u16, u8)> {
    let (migration, step) = target.trim().split_once(':')?;
    let all_digits = |s: &str, n: usize| s.len() == n && s.bytes().all(|b| b.is_ascii_digit());
    if !all_digits(migration, 4) || !all_digits(step, 2) {
        return None;
    }
    Some((migration.parse().ok()?, step.parse().ok()?))
}

/// Plan `ferro migrate rerecord <migration>:<step> [--continue|--restart]`
/// (ADR-0030): the record rewrite that accepts a deliberate edit of one
/// step's file, or the refusal.
///
/// Only the step's own record changes, and only its `file`, `checksum` and
/// `kind` (plus, with `--restart`, its cursor and `rows_done`); nothing is
/// run. `order_keys` are the edited files' order keys ([`OrderKeys`]),
/// which `--continue` and the edited-chunked refusal compare against the
/// cursor's.
///
/// # Errors
/// [`RunRefusal::RerecordTarget`] for anything but `NNNN:NN` (a migration
/// alone, the snapshot: restore it); [`RunRefusal::NothingToRerecord`] for
/// a step the directory lacks, one with no record (free to edit) or one
/// whose file matches its record; [`RunRefusal::ModeNotApplicable`] for
/// `--continue`/`--restart` on anything but a chunked step with committed
/// batches; [`RunRefusal::EditedChunked`] for an unfinished one with
/// neither flag; [`RunRefusal::ContinueRefused`] for `--continue` when the
/// edited file pages over different order keys; and the planner's own
/// refusals about the records and the migration's snapshot.
///
/// A step whose down stopped part-way (its record `reverting`) is an
/// unfinished attempt too: without a flag or with `--continue` its edit is
/// accepted and its revert cursor kept while the edited `down` pages over
/// the cursor's order keys, [`RunRefusal::EditedReverting`] (naming
/// `--restart`) otherwise; `--restart` clears the revert cursor. Another
/// step's reverting record refuses as `up` does: finish that revert first.
pub fn rerecord_plan(
    dir: &MigrationsDir,
    records: &[StepRecord],
    target: &str,
    mode: RerecordMode,
    dialect: Dialect,
    order_keys: &OrderKeys,
) -> Result<RerecordAction, RunRefusal> {
    let (number, ordinal) =
        parse_step_target(target).ok_or_else(|| RunRefusal::RerecordTarget {
            target: target.trim().to_string(),
        })?;
    let shown = format!("{number:04}:{ordinal:02}");
    let nothing = |why: String| RunRefusal::NothingToRerecord {
        target: shown.clone(),
        why,
    };
    check_records(dir, records, AllowAhead::Any, false)?;
    if let Some(other) = records
        .iter()
        .find(|r| r.reverting && (r.migration, r.step) != (number, ordinal))
    {
        return Err(RunRefusal::Reverting { step: other.path() });
    }
    let (migration, step) = dir
        .migrations
        .get(usize::from(number).wrapping_sub(1))
        .and_then(|m| Some((m, m.steps.iter().find(|s| s.ordinal == ordinal)?)))
        .ok_or_else(|| nothing(format!("{}/ holds no such step", directory_label(dir))))?;
    let record = records
        .iter()
        .find(|r| r.migration == number && r.step == ordinal)
        .ok_or_else(|| {
            nothing("the step has no record on this database, so its file is free to edit".into())
        })?;
    let snapshot = encode_checksum(&migration.snapshot.checksum);
    if record.snapshot_checksum != snapshot {
        return Err(RunRefusal::SnapshotMismatch {
            migration_name: migration.dir_name(),
            applied: record.snapshot_checksum.clone(),
            on_disk: snapshot,
        });
    }
    let file = step_file(migration, step, dialect)?;
    let name = file_name(&file.up);
    let checksum = encode_checksum(&file.up_checksum);
    if record.checksum == checksum && record.file == name {
        return Err(nothing(format!(
            "{}/{name} matches its record (sha384:{checksum})",
            migration.dir_name()
        )));
    }
    let data = step.kind == StepKind::Data;
    let kind = if data {
        record.kind
    } else {
        exec_mode(
            &file.headers,
            dialect,
            &format!("{}/{name}", migration.dir_name()),
        )?
        .record_kind()
    };
    let action = RerecordAction {
        migration: number,
        step: ordinal,
        migration_name: migration.dir_name(),
        recorded_file: record.file.clone(),
        file: name.clone(),
        path: file.up.clone(),
        old_checksum: record.checksum.clone(),
        new_checksum: checksum.clone(),
        kind,
        data,
        finished: record.is_finished(),
        clear_cursor: mode == RerecordMode::Restart,
        reverting: record.reverting,
    };
    if record.reverting {
        return rerecord_reverting(record, action, mode, order_keys, shown);
    }
    let batches = has_committed_batches(record);
    if mode != RerecordMode::Record && !batches {
        let state = if record.is_finished() {
            "finished".to_string()
        } else {
            format!(
                "an unfinished {} step with no committed batch",
                record.kind.as_str()
            )
        };
        return Err(RunRefusal::ModeNotApplicable {
            target: shown,
            flag: mode.flag(),
            state,
        });
    }
    if batches {
        let refusal = edited_chunked(record, name.clone(), checksum.clone(), Some(order_keys));
        match (mode, &refusal) {
            (RerecordMode::Record, _) => return Err(refusal),
            (
                RerecordMode::Continue,
                RunRefusal::EditedChunked {
                    continue_allowed: false,
                    keys_recorded,
                    keys_on_disk,
                    ..
                },
            ) => {
                return Err(RunRefusal::ContinueRefused {
                    migration_name: migration.dir_name(),
                    target: shown,
                    file: name,
                    keys_recorded: keys_recorded.clone(),
                    keys_on_disk: keys_on_disk.clone().unwrap_or_default(),
                });
            }
            _ => {}
        }
    }
    Ok(action)
}

/// [`rerecord_plan`] for a step whose down stopped part-way (`record` is
/// reverting), `action` its re-record: `--restart` clears its revert
/// cursor; otherwise its edit is accepted on `--continue`'s condition
/// ([`edited_reverting`]).
fn rerecord_reverting(
    record: &StepRecord,
    action: RerecordAction,
    mode: RerecordMode,
    order_keys: &OrderKeys,
    shown: String,
) -> Result<RerecordAction, RunRefusal> {
    let reverted = record.revert_cursor.is_some();
    if mode != RerecordMode::Record && !reverted {
        return Err(RunRefusal::ModeNotApplicable {
            target: shown,
            flag: mode.flag(),
            state: "reverting with no reverted batch".to_string(),
        });
    }
    if mode != RerecordMode::Restart
        && let Some(refusal) = edited_reverting(record, &action.file, Some(order_keys))
    {
        return Err(refusal);
    }
    Ok(action)
}

/// One step's state, in `sqlx-cli migrate info`'s vocabulary (#466).
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StepState {
    /// Finished, file unchanged.
    Applied,
    /// Finished, but the file on disk changed since.
    AppliedDifferentChecksum,
    /// Recorded by `ferro migrate baseline`.
    AppliedBaseline,
    /// Adopted from the migrations a squash replaced (ADR-0056), its file
    /// unchanged since.
    AppliedAdopted,
    /// No finished record.
    Pending,
    /// No record, in a squash whose witness the records hold whole
    /// (ADR-0056): pending its adoption, which the next run makes, running
    /// nothing.
    PendingAdoptable,
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
            StepState::AppliedDifferentChecksum
                | StepState::Failed
                | StepState::Interrupted
                | StepState::Reverting
        )
    }

    /// Whether the step is still to be applied (`status` exit 3).
    pub fn is_pending(self) -> bool {
        !matches!(
            self,
            StepState::Applied
                | StepState::AppliedDifferentChecksum
                | StepState::AppliedBaseline
                | StepState::AppliedAdopted
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
    /// The step's class (ADR-0054): its record's when it has one (`contract`
    /// for a record an older ferro wrote without one), else the class a run
    /// would record for it.
    pub class: StepClass,
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
    /// For a squash pending its adoption, `NNNN_<name>` of every migration
    /// whose records witness it, in chain order ([`Adoption::replaced`]):
    /// what the database applied. Empty otherwise.
    pub adopts: Vec<String>,
}

/// What `ferro migrate status` reports: every migration's steps, the
/// migrations the database has applied that the directory lacks, and the
/// refusal a run would meet.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct RunStatus {
    /// The directory's migrations, in order.
    pub migrations: Vec<MigrationStatus>,
    /// Applied migrations the directory lacks (`NNNN_<name>`), never one
    /// whose records witness an adoptable squash: adoptable is recognised
    /// before ahead (ADR-0038). A partial witness's records are ahead.
    pub ahead: Vec<String>,
    /// `NNNN_<name>` of every squash the next run adopts ([`adoptable`]),
    /// in directory order: each is pending (ADR-0040).
    pub adoptable: Vec<String>,
    /// What `up` would refuse with, when it would.
    pub refusal: Option<String>,
    /// Whether that refusal is about the database (rather than a capability
    /// this ferro lacks).
    pub refusal_needs_attention: bool,
}

/// Answer `ferro migrate status` (#466) read-only: each step's state from its
/// record, the file on disk and whether a run holds the lock, plus the
/// refusal `up` would meet. `lock_held` makes the first unfinished step
/// `running`. `order_keys` are passed through to [`plan_run`] for the
/// refusal's text.
///
/// Adoptable is recognised before ahead (ADR-0038, ADR-0056): a squash
/// whose witness the records hold whole is `pending_adoptable`, its
/// witnessed records are not ahead, and the refusal is the one the run meets
/// after adopting. A partial witness is ahead by the originals it holds and
/// carries the run's `partial_witness` refusal. A step is read from its own
/// migration's records only, never from an original's of the same number.
pub fn run_status(
    dir: &MigrationsDir,
    records: &[StepRecord],
    dialect: Dialect,
    lock_held: bool,
    order_keys: Option<&OrderKeys>,
) -> RunStatus {
    let adoptions = adoptable(dir, records);
    let partials = partial_witnesses(dir, records);
    let witnessed: BTreeSet<(u16, u8)> = adoptions
        .iter()
        .flat_map(|a| a.witnessed.iter().copied())
        .collect();
    let by_key: RecordMap = records
        .iter()
        .filter(|r| !witnessed.contains(&(r.migration, r.step)))
        .map(|r| ((r.migration, r.step), r))
        .collect();
    let mut running_marked = false;
    let mut migrations = Vec::new();
    for migration in &dir.migrations {
        let name = migration.dir_name();
        let adopts = adoptions
            .iter()
            .find(|a| a.squash == name)
            .map(|a| a.replaced.clone())
            .unwrap_or_default();
        let mut steps = Vec::new();
        let mut classes: Option<BTreeMap<u8, StepClass>> = None;
        for step in &migration.steps {
            let record = by_key
                .get(&(migration.number, step.ordinal))
                .filter(|r| r.migration_name == name);
            let file = step_file(migration, step, dialect).ok();
            let on_disk = file.map(|f| encode_checksum(&f.up_checksum));
            let name = file
                .map(|f| file_name(&f.up))
                .or_else(|| record.map(|r| r.file.clone()))
                .unwrap_or_else(|| format!("{:02}_{}", step.ordinal, step.name));
            let mut state = match record {
                None if !adopts.is_empty() => StepState::PendingAdoptable,
                None => StepState::Pending,
                Some(r) if r.reverting => StepState::Reverting,
                // A finished step carrying an error: its down failed, and the
                // record stands until a `down` reverts it.
                Some(r) if r.is_finished() && r.error.is_some() => StepState::Failed,
                Some(r) if r.is_finished() && r.origin.is_baseline() => StepState::AppliedBaseline,
                Some(r) if r.is_finished() => {
                    if Some(&r.checksum) != on_disk.as_ref() || r.file != name {
                        StepState::AppliedDifferentChecksum
                    } else if r.origin == Origin::Adopted {
                        StepState::AppliedAdopted
                    } else {
                        StepState::Applied
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
            let class = match record {
                Some(r) => r.class_or_contract(),
                // A migration that does not regenerate shows `contract`; the
                // run's refusal below names it.
                None => classes
                    .get_or_insert_with(|| {
                        migration_classes(dir, migration, dialect).unwrap_or_default()
                    })
                    .get(&step.ordinal)
                    .copied()
                    .unwrap_or(StepClass::Contract),
            };
            steps.push(StepStatus {
                step: step.ordinal,
                file: name,
                state,
                error: record.and_then(|r| r.error.clone()),
                applied_checksum: record.map(|r| r.checksum.clone()),
                on_disk_checksum: on_disk,
                flags,
                class,
            });
        }
        migrations.push(MigrationStatus {
            number: migration.number,
            name,
            steps,
            adopts,
        });
    }
    let head = dir.head().map_or(0, |m| m.number);
    let partly_witnessed: BTreeSet<&str> = partials
        .iter()
        .flat_map(|p| p.satisfied.iter().chain(&p.missing))
        .map(String::as_str)
        .collect();
    let ahead: BTreeSet<String> = records
        .iter()
        .filter(|r| !witnessed.contains(&(r.migration, r.step)))
        .filter(|r| r.migration > head || partly_witnessed.contains(r.migration_name.as_str()))
        .map(|r| r.migration_name.clone())
        .collect();
    // The refusal the run meets: its adoption's, else its plan's over the
    // records the adoption leaves. A data step's kind decides none.
    let refusal = adoption(dir, records, dialect, &BTreeMap::new(), "", "")
        .and_then(|adopted| {
            plan_run(
                dir,
                &adopted.records,
                dialect,
                Direction::Up { through: None },
                false,
                order_keys,
            )
        })
        .err();
    RunStatus {
        migrations,
        ahead: ahead.into_iter().collect(),
        adoptable: adoptions.into_iter().map(|a| a.squash).collect(),
        refusal_needs_attention: refusal.as_ref().is_some_and(RunRefusal::needs_attention),
        refusal: refusal.map(|r| r.to_string()),
    }
}

/// The class of every record `records` holds without one (written by an
/// older ferro), by `(migration, step)`: what the step's migration in `dir`
/// decides ([`step_class::migration_classes`]) when the directory holds the
/// files the record applied (the migration under the record's name and
/// snapshot, the step's file under the record's name and checksum),
/// `contract` when it does not: the class is what the applied step did, and
/// an ahead record of unknown class is unsafe (ADR-0054). A migration
/// replaced in place, or a step edited or renamed after it was recorded, is
/// not what was applied. What every mutating verb writes under the run lock;
/// readers never write it. A record whose applied migration does not
/// regenerate from its snapshots gets no class: its migration's
/// [`RunRefusal::Unclassifiable`] is in [`Unclassified::undecided`], for
/// the verb to refuse on (`up`, `baseline`) or to warn of (a verb that
/// reads no class).
pub fn unclassified(dir: &MigrationsDir, records: &[StepRecord], dialect: Dialect) -> Unclassified {
    let mut by_migration: BTreeMap<u16, Option<BTreeMap<u8, StepClass>>> = BTreeMap::new();
    let mut out = Unclassified::default();
    for record in records.iter().filter(|record| record.class.is_none()) {
        let mut class = Some(StepClass::Contract);
        if let Some(migration) = applied_migration(dir, record, dialect) {
            let decided = by_migration.entry(migration.number).or_insert_with(|| {
                match migration_classes(dir, migration, dialect) {
                    Ok(classes) => Some(classes),
                    Err(refusal) => {
                        out.undecided.push(refusal);
                        None
                    }
                }
            });
            class = decided.as_ref().map(|classes| {
                classes
                    .get(&record.step)
                    .copied()
                    .unwrap_or(StepClass::Contract)
            });
        }
        if let Some(class) = class {
            out.classes.push((record.migration, record.step, class));
        }
    }
    out
}

/// What [`unclassified`] decides for the records without a class.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Unclassified {
    /// Each `(migration, step, class)` to record.
    pub classes: Vec<(u16, u8, StepClass)>,
    /// Each applied migration that does not regenerate, whose records keep
    /// no class ([`RunRefusal::Unclassifiable`]), once.
    pub undecided: Vec<RunRefusal>,
}

impl RunRefusal {
    /// The warning a verb that reads no class gives for an
    /// [`RunRefusal::Unclassifiable`] migration whose records it leaves
    /// without one; `None` for any other refusal.
    pub fn deferred_class_warning(&self) -> Option<String> {
        match self {
            RunRefusal::Unclassifiable {
                migration_name,
                error,
                ..
            } => Some(format!(
                "ferro migrate: {migration_name} does not regenerate from its snapshots, so \
                 its records keep no class and read as contract (ADR-0054): {error}\nThe next \
                 `ferro migrate up` or `ferro migrate baseline` classifies them, or refuses \
                 naming the fix."
            )),
            _ => None,
        }
    }
}

/// The migration of `dir` holding the files `record` applied on `dialect`:
/// its number, name and snapshot, and its step's file name and checksum, as
/// the record names them.
fn applied_migration<'a>(
    dir: &'a MigrationsDir,
    record: &StepRecord,
    dialect: Dialect,
) -> Option<&'a Migration> {
    let migration = dir.migrations.iter().find(|m| {
        m.number == record.migration
            && m.dir_name() == record.migration_name
            && encode_checksum(&m.snapshot.checksum) == record.snapshot_checksum
    })?;
    let step = migration.steps.iter().find(|s| s.ordinal == record.step)?;
    let file = step_file(migration, step, dialect).ok()?;
    (file_name(&file.up) == record.file && encode_checksum(&file.up_checksum) == record.checksum)
        .then_some(migration)
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
    /// A SQLite back-quoted identifier (`` `end` ``).
    Backtick,
    /// A SQLite bracket-quoted identifier (`[my end]`).
    Bracket,
    LineComment,
    BlockComment,
}

/// Where a statement stands on a compound body: one whose own `;`s end
/// inner statements, never the outer one (a SQLite trigger's
/// `BEGIN … END`, a Postgres `BEGIN ATOMIC … END` function body).
///
/// Neither body can nest another `BEGIN` (a trigger body and a `BEGIN
/// ATOMIC` body hold plain statements only), so once the body is open only
/// `CASE` … `END` nests inside it: a `begin` column in the body is a name,
/// never a second opener.
struct Block {
    dialect: Dialect,
    /// The statement's first keywords, upper-cased (at most three: enough to
    /// read `CREATE [TEMP|TEMPORARY] TRIGGER`).
    leading: Vec<String>,
    /// The keyword before the current one (`BEGIN` before `ATOMIC`).
    previous: Option<String>,
    /// The statement is a SQLite `CREATE TRIGGER`, whose first `BEGIN`
    /// opens its body.
    trigger: bool,
    /// The body is open: `;` no longer ends the statement until `depth`
    /// returns to 0.
    open: bool,
    /// Unclosed `BEGIN` (the body's own) and `CASE` keywords.
    depth: usize,
}

impl Block {
    fn new(dialect: Dialect) -> Self {
        Block {
            dialect,
            leading: Vec::new(),
            previous: None,
            trigger: false,
            open: false,
            depth: 0,
        }
    }

    /// A `;` here ends the statement.
    fn ends_at_semicolon(&self) -> bool {
        self.depth == 0
    }

    fn word(&mut self, word: &str) {
        let word = word.to_uppercase();
        let leading = self.leading.len() < 3;
        if leading {
            self.leading.push(word.clone());
        }
        match word.as_str() {
            "CASE" if self.open || self.trigger => self.depth += 1,
            "END" if self.open || self.trigger => self.depth = self.depth.saturating_sub(1),
            "BEGIN" if self.trigger && !self.open => {
                self.open = true;
                self.depth += 1;
            }
            "ATOMIC"
                if self.dialect == Dialect::Postgres
                    && !self.open
                    && self.previous.as_deref() == Some("BEGIN") =>
            {
                // The `BEGIN` just read opens the body.
                self.open = true;
                self.depth += 1;
            }
            _ if leading && self.dialect == Dialect::Sqlite && self.opens_trigger() => {
                self.trigger = true;
            }
            _ => {}
        }
        self.previous = Some(word);
    }

    /// The leading keywords read so far are `CREATE [TEMP|TEMPORARY] TRIGGER`.
    fn opens_trigger(&self) -> bool {
        match self.leading.as_slice() {
            [create, trigger] => create == "CREATE" && trigger == "TRIGGER",
            [create, temp, trigger] => {
                create == "CREATE"
                    && (temp == "TEMP" || temp == "TEMPORARY")
                    && trigger == "TRIGGER"
            }
            _ => false,
        }
    }
}

/// Cut a SQL step file into the statements the executor sends one at a time.
///
/// Not a SQL parser: a statement ends at a `;` outside a quoted string, a
/// quoted identifier, a comment and a dollar-quoted body (`DO $$ … $$`,
/// `$tag$ … $tag$`). A chunk holding only comments and whitespace (the
/// `-- ferro:` header lines, a `not-applicable` file) is no statement.
///
/// A compound body keeps its inner `;`s: on SQLite a statement that begins
/// `CREATE [TEMP|TEMPORARY] TRIGGER` opens its body at its first `BEGIN`,
/// and on Postgres `BEGIN ATOMIC` opens a SQL-standard function body. Inside
/// the body `CASE` … `END` nest and the body's own `END` closes it; a `;`
/// ends the statement only once they balance. Keywords are whole words
/// outside strings, quoted identifiers (`"…"`, and on SQLite `` `…` `` and
/// `[…]`), comments and dollar bodies, and never a qualified name's part
/// (`new.end`). A plain `BEGIN;` anywhere else is a statement of its own.
pub fn split_statements(sql: &str, dialect: Dialect) -> Vec<String> {
    let mut statements = Vec::new();
    let mut current = String::new();
    let mut has_code = false;
    let mut state = Lexical::Code;
    let mut dollar: Option<String> = None;
    let mut block = Block::new(dialect);
    let chars: Vec<char> = sql.chars().collect();
    // Identifier characters, non-ASCII letters among them (`éend` is one
    // word, not `é` then `end`).
    let is_word = |c: char| c.is_alphanumeric() || c == '_';
    let sqlite = dialect == Dialect::Sqlite;
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
                ';' if block.ends_at_semicolon() => {
                    if has_code {
                        statements.push(current.trim().to_string());
                    }
                    current.clear();
                    has_code = false;
                    block = Block::new(dialect);
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
                '`' if sqlite => {
                    state = Lexical::Backtick;
                    has_code = true;
                }
                '[' if sqlite => {
                    state = Lexical::Bracket;
                    has_code = true;
                }
                c if (c.is_alphabetic() || c == '_')
                    && !i
                        .checked_sub(1)
                        .is_some_and(|at| is_word(chars[at]) || chars[at] == '$') =>
                {
                    let end = (i..chars.len())
                        .find(|&at| !is_word(chars[at]))
                        .unwrap_or(chars.len());
                    let word: String = chars[i..end].iter().collect();
                    // `new.end` names a column, never a keyword.
                    let qualified = i > 0 && chars[i - 1] == '.';
                    if !qualified {
                        block.word(&word);
                    }
                    current.push_str(&word);
                    has_code = true;
                    i = end;
                    continue;
                }
                c if !c.is_whitespace() => has_code = true,
                _ => {}
            },
            Lexical::Single if c == '\'' => state = Lexical::Code,
            Lexical::Double if c == '"' => state = Lexical::Code,
            Lexical::Backtick if c == '`' => state = Lexical::Code,
            Lexical::Bracket if c == ']' => state = Lexical::Code,
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
            let bytes = Snapshot::store(&ir(tables), parent, &[]).expect("store");
            out.push(Migration {
                number: u16::try_from(i + 1).expect("number"),
                name: name.to_string(),
                dir: PathBuf::from(format!("/migrations/{:04}_{name}", i + 1)),
                steps,
                witness: Vec::new(),
                snapshot: Snapshot::load(&bytes).expect("load"),
            });
        }
        MigrationsDir {
            path: PathBuf::from("/proj/migrations"),
            migrations: out,
        }
    }

    /// `dir` with each DDL step's files holding what the generator renders
    /// for it from the migration's two snapshots, as `ferro migrate new`
    /// writes them: the files whose class the generator decides (ADR-0054).
    fn as_generated(mut dir: MigrationsDir) -> MigrationsDir {
        let snapshots: Vec<Snapshot> = dir.migrations.iter().map(|m| m.snapshot.clone()).collect();
        for (i, migration) in dir.migrations.iter_mut().enumerate() {
            let parent = i.checked_sub(1).map(|p| &snapshots[p]);
            let generated = crate::generate::generate(
                parent,
                &migration.snapshot.ir,
                &[Dialect::Postgres, Dialect::Sqlite],
            )
            .expect("generates")
            .expect("changes something");
            for step in migration
                .steps
                .iter_mut()
                .filter(|s| s.kind == StepKind::Ddl)
            {
                let g = generated
                    .steps
                    .iter()
                    .find(|g| g.name == step.name)
                    .expect("generated");
                for (on, rendering) in &g.renderings {
                    let dialect = match on {
                        StepDialect::Postgres => "postgres",
                        StepDialect::Sqlite => "sqlite",
                        StepDialect::Portable => continue,
                    };
                    step.files.insert(
                        *on,
                        sql_file(dialect, step.ordinal, &step.name, &rendering.up, DOWN),
                    );
                }
            }
        }
        dir
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
        let plan = plan_run(
            dir,
            &[],
            Dialect::Sqlite,
            Direction::Up { through: None },
            false,
            None,
        )
        .expect("plan");
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
        plan_run(
            dir,
            records,
            Dialect::Sqlite,
            Direction::Up { through: None },
            false,
            None,
        )
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
                 file, or accept a deliberate edit with\n`ferro migrate rerecord 0001:01`. \
                 Nothing was applied.",
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
                 models are built from it.\nRestore the file, or 0001 was replaced: revert \
                 it with the files that were applied (check out the commit that applied it, \
                 `ferro migrate down --to 0000`), then `up`. Nothing was applied.",
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
        let plan = plan_run(
            &behind,
            &records,
            Dialect::Sqlite,
            Direction::Up { through: None },
            true,
            None,
        )
        .expect("allowed");
        assert!(plan.steps.is_empty());
        assert_eq!(plan.ahead, ["0003_add_orgs"]);
    }

    #[test]
    fn a_record_whose_migration_was_renamed_on_disk_is_missing_even_when_ahead_is_allowed() {
        let dir = three();
        let mut record = finished(&dir, 1, 1);
        record.migration_name = "0001_other".into();
        let refusal = plan_run(
            &dir,
            &[record],
            Dialect::Sqlite,
            Direction::Up { through: None },
            true,
            None,
        )
        .expect_err("missing");
        assert_eq!(refusal.kind(), "applied_missing");
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: this database has applied 0001_other, which is not in \
             migrations/.\nThe directory is behind the database: check out the branch that \
             holds it, or 0001 was replaced: revert it with the files that were applied \
             (check out the commit that applied it, `ferro migrate down --to 0000`), then \
             `up`. Nothing was applied."
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

    // -- edited files and rerecord (#537, ADR-0030) ------------------------------------

    const CURSOR: &str = r#"{"keys": [1000], "order_by": ["author.id"], "rows_done": 1000}"#;
    const OTHER: &str = "0000000000000000000000000000000000000000000000000000000000000000\
                         00000000000000000000000000000000";

    fn order_keys(keys: &[&str]) -> OrderKeys {
        BTreeMap::from([((1, 2), keys.iter().map(|k| k.to_string()).collect())])
    }

    /// `0001_backfill:02`'s record: an attempt of the chunked backfill that
    /// committed 1,000 rows, under a file whose checksum was `OTHER`.
    fn chunked_with_batches(dir: &MigrationsDir) -> StepRecord {
        StepRecord {
            kind: RecordKind::Chunked,
            checksum: OTHER.into(),
            resume_cursor: Some(CURSOR.into()),
            rows_done: Some(1000),
            ..started(dir, 1, 2)
        }
    }

    fn edited_chunked_text(dir: &MigrationsDir) -> String {
        let records = [finished(dir, 1, 1), chunked_with_batches(dir)];
        plan_run(
            dir,
            &records,
            Dialect::Sqlite,
            Direction::Up { through: None },
            false,
            Some(&order_keys(&["author.id"])),
        )
        .expect_err("edited chunked")
        .to_string()
    }

    #[test]
    fn an_edited_chunked_step_with_committed_batches_is_refused_naming_both_doors() {
        let dir = backfill();
        assert_eq!(
            edited_chunked_text(&dir),
            "ferro migrate: 0001_backfill/02_backfill_author.py was edited after 1,000 rows \
             were committed.\nFerro cannot tell whether those rows are right under the new \
             code. Choose one:\n  ferro migrate rerecord 0001:02 --continue   keep them, \
             continue from the cursor\n  ferro migrate rerecord 0001:02 --restart    run every \
             row again from the start\nNothing was applied."
        );
        let refusal =
            up(&dir, &[finished(&dir, 1, 1), chunked_with_batches(&dir)]).expect_err("unread keys");
        assert_eq!(refusal.kind(), "edited_chunked");
        assert_eq!((refusal.migration(), refusal.step()), (Some(1), Some(2)));
        assert!(refusal.to_string().contains(
            "continue from the cursor\n    (only while the edited file still pages over \
             author.id)\n"
        ));
    }

    #[test]
    fn an_edited_chunked_step_over_other_order_keys_offers_only_a_restart() {
        let dir = backfill();
        let records = [finished(&dir, 1, 1), chunked_with_batches(&dir)];
        let plan = |keys: &[&str]| {
            plan_run(
                &dir,
                &records,
                Dialect::Sqlite,
                Direction::Up { through: None },
                false,
                Some(&order_keys(keys)),
            )
            .expect_err("refused")
            .to_string()
        };
        assert_eq!(
            plan(&["author.slug", "author.id"]),
            "ferro migrate: 0001_backfill/02_backfill_author.py was edited after 1,000 rows \
             were committed.\nFerro cannot tell whether those rows are right under the new \
             code, and the edited file pages over different order keys than its cursor:\n  \
             cursor    author.id\n  on disk   author.slug, author.id\nso the cursor is no \
             position in it. Restart from the first row:\n  ferro migrate rerecord 0001:02 \
             --restart    run every row again from the start\nNothing was applied."
        );
        assert!(plan(&[]).contains(
            "and the edited file's up is no longer @chunked:\n  cursor    author.id\n  on disk   \
             (none)\n"
        ));
    }

    #[test]
    fn an_edited_chunked_step_with_no_committed_batch_is_accepted_like_any_unfinished_step() {
        let dir = backfill();
        let attempt = StepRecord {
            resume_cursor: None,
            rows_done: Some(0),
            ..chunked_with_batches(&dir)
        };
        let plan = up(&dir, &[finished(&dir, 1, 1), attempt]).expect("accepted");
        assert_eq!(
            plan.steps[0].edited.as_ref().expect("edited").recorded,
            OTHER
        );
    }

    #[test]
    fn an_unedited_chunked_step_with_committed_batches_just_resumes() {
        let dir = backfill();
        let attempt = StepRecord {
            checksum: encode_checksum(&sha384(b"# data")),
            ..chunked_with_batches(&dir)
        };
        let plan = up(&dir, &[finished(&dir, 1, 1), attempt]).expect("resumes");
        assert!(plan.steps[0].resumes && plan.steps[0].edited.is_none());
    }

    #[test]
    fn a_cursor_records_the_order_keys_it_was_committed_under() {
        assert_eq!(cursor_order_keys(CURSOR), ["author.id"]);
        assert!(cursor_order_keys(r#"{"keys": [1], "rows_done": 1}"#).is_empty());
        assert!(cursor_order_keys("not json").is_empty());
    }

    // -- a chunked down that stopped part-way (F17) ---------------------------------

    /// `0001_backfill:02` finished, then its chunked down reverted 1,000 rows
    /// and stopped: `reverting` at a revert cursor, under a file whose
    /// checksum was `OTHER`.
    fn reverting_edited(dir: &MigrationsDir) -> StepRecord {
        StepRecord {
            kind: RecordKind::Chunked,
            checksum: OTHER.into(),
            reverting: true,
            revert_cursor: Some(CURSOR.into()),
            rows_done: Some(1000),
            ..finished(dir, 1, 2)
        }
    }

    fn down_with_keys(
        dir: &MigrationsDir,
        records: &[StepRecord],
        keys: &[&str],
    ) -> Result<RunPlan, RunRefusal> {
        plan_run(
            dir,
            records,
            Dialect::Sqlite,
            Direction::Down {
                target: Target::Migration(0),
            },
            false,
            Some(&order_keys(keys)),
        )
    }

    #[test]
    fn a_reverting_step_edited_over_the_same_keys_is_accepted_and_re_recorded() {
        let dir = backfill();
        let records = [finished(&dir, 1, 1), reverting_edited(&dir)];
        let plan = down_with_keys(&dir, &records, &["author.id"]).expect("accepted");
        let step = &plan.steps[0];
        assert_eq!((step.migration, step.step), (1, 2));
        assert_eq!(step.edited.as_ref().expect("edited").recorded, OTHER);
        // The record takes the edited file's checksum with its next write,
        // and keeps the cursor the down resumes at.
        assert_eq!(step.record.checksum, encode_checksum(&sha384(b"# data")));
        assert_eq!(step.record.revert_cursor.as_deref(), Some(CURSOR));
        // Keys not read: accepted, the run's own cursor decode decides.
        assert!(down(&dir, &records, Target::Migration(0)).is_ok());
    }

    #[test]
    fn a_reverting_step_edited_over_other_keys_is_refused_naming_restart() {
        let dir = backfill();
        let records = [finished(&dir, 1, 1), reverting_edited(&dir)];
        let refusal =
            down_with_keys(&dir, &records, &["author.name", "author.id"]).expect_err("refused");
        assert_eq!(refusal.kind(), "edited_reverting");
        assert_eq!((refusal.migration(), refusal.step()), (Some(1), Some(2)));
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: 0001_backfill/02_backfill_author.py was edited while its down was \
             part-way: 1,000 rows were reverted under the order keys author.id, and the edited \
             down pages over author.name, author.id, so its cursor is no position in it. \
             Revert the remaining rows from the first:\n  ferro migrate rerecord 0001:02 \
             --restart\nNothing was reverted."
        );
        let not_chunked = down_with_keys(&dir, &records, &[]).expect_err("refused");
        assert!(
            not_chunked
                .to_string()
                .contains("and the edited down is no longer @chunked, so")
        );
    }

    #[test]
    fn an_edited_applied_step_met_going_down_says_nothing_was_reverted() {
        let dir = backfill();
        let edited = StepRecord {
            checksum: OTHER.into(),
            ..finished(&dir, 1, 2)
        };
        let refusal = down(
            &dir,
            &[finished(&dir, 1, 1), edited.clone()],
            Target::Migration(0),
        )
        .expect_err("edited");
        assert_eq!(refusal.kind(), "edited_applied");
        assert!(refusal.to_string().ends_with("Nothing was reverted."));
        let refusal = up(&dir, &[finished(&dir, 1, 1), edited]).expect_err("edited");
        assert!(refusal.to_string().ends_with("Nothing was applied."));
    }

    #[test]
    fn rerecord_accepts_a_reverting_step_over_its_keys_and_restart_clears_its_cursor() {
        let dir = backfill();
        let records = [finished(&dir, 1, 1), reverting_edited(&dir)];
        let plan = |mode, keys: &[&str]| {
            rerecord_plan(
                &dir,
                &records,
                "0001:02",
                mode,
                Dialect::Sqlite,
                &order_keys(keys),
            )
        };
        for mode in [RerecordMode::Record, RerecordMode::Continue] {
            let action = plan(mode, &["author.id"]).expect("accepted");
            assert!(action.reverting && !action.clear_cursor);
            assert_eq!(
                plan(mode, &["author.name", "author.id"])
                    .expect_err("other keys")
                    .kind(),
                "edited_reverting"
            );
        }
        let restart = plan(RerecordMode::Restart, &["author.name", "author.id"]).expect("restart");
        assert!(restart.reverting && restart.clear_cursor);
        // Once cleared, there is no cursor to continue or restart.
        let cleared = [
            finished(&dir, 1, 1),
            StepRecord {
                revert_cursor: None,
                ..reverting_edited(&dir)
            },
        ];
        let refusal = rerecord_plan(
            &dir,
            &cleared,
            "0001:02",
            RerecordMode::Continue,
            Dialect::Sqlite,
            &order_keys(&["author.id"]),
        )
        .expect_err("no cursor");
        assert_eq!(refusal.kind(), "mode_not_applicable");
    }

    #[test]
    fn rerecord_of_another_step_still_refuses_while_a_revert_is_part_way() {
        let dir = backfill();
        let records = [
            StepRecord {
                checksum: OTHER.into(),
                ..finished(&dir, 1, 1)
            },
            reverting_edited(&dir),
        ];
        let refusal = rerecord_plan(
            &dir,
            &records,
            "0001:01",
            RerecordMode::Record,
            Dialect::Sqlite,
            &OrderKeys::new(),
        )
        .expect_err("reverting");
        assert_eq!(refusal.kind(), "reverting");
    }

    /// The edited record of `kind` at `0001:01` (SQL kinds) or `0001:02`
    /// (data kinds), finished or not; `batches` gives a chunked one a cursor.
    fn edited(dir: &MigrationsDir, kind: RecordKind, finished_: bool, batches: bool) -> StepRecord {
        let step = if matches!(kind, RecordKind::Ddl | RecordKind::DdlNoTransaction) {
            1
        } else {
            2
        };
        let base = finished(dir, 1, step);
        StepRecord {
            kind,
            checksum: OTHER.into(),
            finished_at: finished_.then(|| base.finished_at.clone()).flatten(),
            resume_cursor: batches.then(|| CURSOR.to_string()),
            rows_done: (kind == RecordKind::Chunked).then_some(if batches { 1000 } else { 0 }),
            ..base
        }
    }

    #[test]
    fn rerecord_plans_every_kind_finished_and_mode() {
        use RecordKind::*;
        use RerecordMode::*;
        let dir = backfill();
        let same = order_keys(&["author.id"]);
        let changed = order_keys(&["author.slug", "author.id"]);
        let cases = [
            (Ddl, false),
            (DdlNoTransaction, false),
            (Atomic, false),
            (Chunked, false),
            (Chunked, true),
        ];
        for (kind, batches) in cases {
            for finished_ in [true, false] {
                if finished_ && batches {
                    continue; // a finished record's cursor is no committed batch
                }
                let record = edited(&dir, kind, finished_, batches);
                let target = format!("0001:{:02}", record.step);
                let mut records = vec![record.clone()];
                if record.step == 2 {
                    records.insert(0, finished(&dir, 1, 1));
                }
                let plan = |mode, keys: &OrderKeys| {
                    rerecord_plan(&dir, &records, &target, mode, Dialect::Sqlite, keys)
                };
                let cell = format!("{kind:?} finished={finished_} batches={batches}");
                let open_with_batches = batches && !finished_;

                // Record: accepted, except a chunked step with committed batches.
                match plan(Record, &same) {
                    Ok(action) => {
                        assert!(!open_with_batches, "{cell}: record must name both doors");
                        assert_eq!(action.old_checksum, OTHER, "{cell}");
                        assert_eq!(action.new_checksum, plan_step_checksum(&dir, record.step));
                        assert!(!action.clear_cursor, "{cell}");
                        assert_eq!(action.finished, finished_, "{cell}");
                        assert_eq!(action.data, record.step == 2, "{cell}");
                    }
                    Err(refusal) => {
                        assert!(open_with_batches, "{cell}: {refusal}");
                        assert_eq!(refusal.kind(), "edited_chunked", "{cell}");
                    }
                }
                for mode in [Continue, Restart] {
                    let result = plan(mode, &same);
                    if !open_with_batches {
                        let refusal = result.expect_err(&cell);
                        assert_eq!(refusal.kind(), "mode_not_applicable", "{cell}");
                        continue;
                    }
                    let action = result.expect(&cell);
                    assert_eq!(action.clear_cursor, mode == Restart, "{cell}");
                }
                if open_with_batches {
                    let refusal = plan(Continue, &changed).expect_err("changed keys");
                    assert_eq!(refusal.kind(), "continue_refused");
                    assert!(plan(Restart, &changed).expect("restart").clear_cursor);
                }
            }
        }
    }

    fn plan_step_checksum(dir: &MigrationsDir, step: u8) -> String {
        finished(dir, 1, step).checksum
    }

    #[test]
    fn rerecord_texts() {
        let dir = backfill();
        let records = [
            finished(&dir, 1, 1),
            edited(&dir, RecordKind::Chunked, false, true),
        ];
        let keys = order_keys(&["author.slug", "author.id"]);
        let text = |target: &str, mode| {
            rerecord_plan(&dir, &records, target, mode, Dialect::Sqlite, &keys)
                .expect_err(target)
                .to_string()
        };
        for target in ["0001", "0001:ir", "1:2", "0001:02:x"] {
            assert_eq!(
                text(target, RerecordMode::Record),
                format!(
                    "ferro migrate: rerecord re-records one step, named <migration>:<step> \
                     (0007:01), not `{target}`.\nA schema snapshot (ir.json) is never \
                     re-recorded: later migrations and historical models are built from it, so \
                     restore the file. Nothing was changed."
                )
            );
        }
        assert_eq!(
            text("0001:02", RerecordMode::Continue),
            "ferro migrate: cannot continue 0001_backfill/02_backfill_author.py from its cursor: \
             the edited file pages over different order keys than its cursor.\n  cursor    \
             author.id\n  on disk   author.slug, author.id\nThe cursor is no position in the \
             edited query. Restart it with `ferro migrate rerecord 0001:02 --restart`. Nothing \
             was changed."
        );
        assert_eq!(
            text("0001:01", RerecordMode::Restart),
            "ferro migrate: nothing to re-record at 0001:01: \
             0001_backfill/01_schema.up.sqlite.sql matches its record \
             (sha384:"
                .to_string()
                + &plan_step_checksum(&dir, 1)
                + "). Nothing was changed."
        );
        assert_eq!(
            text("0001:07", RerecordMode::Record),
            "ferro migrate: nothing to re-record at 0001:07: migrations/ holds no such step. \
             Nothing was changed."
        );
        let finished_edit = [edited(&dir, RecordKind::Ddl, true, false)];
        let refusal = rerecord_plan(
            &dir,
            &finished_edit,
            "0001:01",
            RerecordMode::Restart,
            Dialect::Sqlite,
            &keys,
        )
        .expect_err("finished");
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: --restart applies only to an unfinished chunked step with committed \
             batches, and 0001:01 is finished.\nAccept its edit with `ferro migrate rerecord \
             0001:01`. Nothing was changed."
        );
        let refusal = rerecord_plan(
            &dir,
            &[finished(&dir, 1, 1)],
            "0001:02",
            RerecordMode::Record,
            Dialect::Sqlite,
            &keys,
        )
        .expect_err("no record");
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: nothing to re-record at 0001:02: the step has no record on this \
             database, so its file is free to edit. Nothing was changed."
        );
    }

    #[test]
    fn the_checksum_covers_only_this_dialects_up_file() {
        // A step whose down and sqlite rendering changed after a Postgres
        // database applied it: neither is a mismatch there.
        let body = "CREATE TABLE a (id int);\n";
        let mut step = ddl(1, "schema", body);
        let applied = |step: &Step| {
            let dir = dir(vec![("a", vec![step.clone()], &["a"])]);
            let plan = plan_run(
                &dir,
                &[],
                Dialect::Postgres,
                Direction::Up { through: None },
                false,
                None,
            )
            .expect("plan");
            let record = StepRecord {
                finished_at: Some("2026-10-01T14:02:33.000000Z".into()),
                ..plan.steps[0].record.clone()
            };
            (dir, record)
        };
        let (_, record) = applied(&step);
        let sqlite = step.files.get_mut(&StepDialect::Sqlite).expect("sqlite");
        sqlite.up_checksum = sha384(b"CREATE TABLE a (id bigint);\n");
        let postgres = step
            .files
            .get_mut(&StepDialect::Postgres)
            .expect("postgres");
        postgres.down_checksum = Some(sha384(b"DROP TABLE a CASCADE;\n"));
        let (dir, _) = applied(&step);
        let plan = plan_run(
            &dir,
            std::slice::from_ref(&record),
            Dialect::Postgres,
            Direction::Up { through: None },
            false,
            None,
        )
        .expect("no mismatch");
        assert!(plan.steps.is_empty());
        let refusal = rerecord_plan(
            &dir,
            &[record],
            "0001:01",
            RerecordMode::Record,
            Dialect::Postgres,
            &OrderKeys::new(),
        )
        .expect_err("nothing");
        assert_eq!(refusal.kind(), "nothing_to_rerecord");
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
            None,
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
            None,
        )
        .expect_err("ahead");
        assert_eq!(refusal.kind(), "applied_missing");
    }

    #[test]
    fn targets_read_and_write_the_json_the_cli_sends() {
        let parse = |json: &str| serde_json::from_str::<Direction>(json).expect(json);
        assert_eq!(
            parse(r#"{"direction": "up"}"#),
            Direction::Up { through: None }
        );
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
        let status = run_status(&dir, &records, Dialect::Sqlite, false, None);
        let states: Vec<StepState> = status.migrations[2].steps.iter().map(|s| s.state).collect();
        assert_eq!(
            states,
            [StepState::Applied, StepState::Applied, StepState::Failed]
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
        let plan = plan_run(
            &dir,
            &[],
            Dialect::Postgres,
            Direction::Up { through: None },
            false,
            None,
        )
        .expect("plan");
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
        let first = Snapshot::store(&ir(&["a"]), None, &[]).expect("store");
        let second = Snapshot::store(&ir(&["a", "b"]), Some([7u8; 48]), &[]).expect("store");
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
        let status = run_status(&dir, &records, Dialect::Sqlite, false, None);
        let states: Vec<Vec<StepState>> = status
            .migrations
            .iter()
            .map(|m| m.steps.iter().map(|s| s.state).collect())
            .collect();
        assert_eq!(
            states,
            [
                vec![StepState::Applied],
                vec![StepState::Applied, StepState::Failed, StepState::Pending]
            ]
        );
        assert_eq!(
            status.migrations[1].steps[1].error.as_deref(),
            Some("no such table: x")
        );
        assert_eq!(status.migrations[1].steps[1].flags, ["destructive"]);
        assert!(status.refusal.is_none());

        let interrupted = vec![finished(&dir, 1, 1), started(&dir, 2, 1)];
        let status = run_status(&dir, &interrupted, Dialect::Sqlite, false, None);
        assert_eq!(status.migrations[1].steps[0].state, StepState::Interrupted);
        let status = run_status(&dir, &interrupted, Dialect::Sqlite, true, None);
        assert_eq!(status.migrations[1].steps[0].state, StepState::Running);
        assert_eq!(status.migrations[1].steps[1].state, StepState::Pending);

        let mut baseline = finished(&dir, 1, 1);
        baseline.origin = Origin::Baseline;
        let mut edited = finished(&dir, 2, 1);
        edited.checksum = "c".repeat(96);
        let status = run_status(&dir, &[baseline, edited], Dialect::Sqlite, false, None);
        assert_eq!(
            status.migrations[0].steps[0].state,
            StepState::AppliedBaseline
        );
        assert_eq!(
            status.migrations[1].steps[0].state,
            StepState::AppliedDifferentChecksum
        );
        assert!(
            status
                .refusal
                .as_deref()
                .is_some_and(|r| r.contains("rerecord"))
        );
        assert!(status.refusal_needs_attention);
    }

    // -- up's through (ADR-0045) --------------------------------------------------------

    fn up_through(
        dir: &MigrationsDir,
        records: &[StepRecord],
        through: u16,
    ) -> Result<RunPlan, RunRefusal> {
        plan_run(
            dir,
            records,
            Dialect::Sqlite,
            Direction::Up {
                through: Some(through),
            },
            false,
            None,
        )
    }

    #[test]
    fn through_is_a_migration_number_and_never_a_step() {
        assert_eq!(parse_through("0007"), Ok(7));
        assert_eq!(parse_through(" 0012 "), Ok(12));
        for written in ["0007:02", "7", "00007", "abcd", ""] {
            let refusal = parse_through(written).expect_err(written);
            assert_eq!(refusal.kind(), "not_a_migration");
            assert_eq!(
                refusal.to_string(),
                format!(
                    "ferro migrate: through {} is not a migration; write a migration number \
                     (0007). A step (0007:02) is not a target: no snapshot describes the state \
                     between two steps. Nothing was applied.",
                    written.trim()
                )
            );
        }
        let parse = |json: &str| serde_json::from_str::<Direction>(json).expect(json);
        assert_eq!(
            parse(r#"{"direction": "up", "through": 7}"#),
            Direction::Up { through: Some(7) }
        );
    }

    #[test]
    fn through_plans_only_the_migrations_up_to_it() {
        let dir = three();
        assert_eq!(
            keys(&up_through(&dir, &[], 2).expect("plan")),
            [(1, 1), (2, 1)]
        );
        assert_eq!(
            keys(&up_through(&dir, &[], 3).expect("plan")),
            keys(&up(&dir, &[]).expect("plan"))
        );
    }

    #[test]
    fn a_through_the_directory_lacks_is_refused_naming_it() {
        let refusal = up_through(&three(), &[], 9).expect_err("no such");
        assert_eq!(refusal.kind(), "no_such_migration");
        assert_eq!(refusal.migration(), Some(9));
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: there is no migration 0009 in /proj/migrations. Nothing was applied."
        );
    }

    #[test]
    fn a_through_at_or_below_the_last_applied_migration_plans_nothing() {
        let dir = three();
        let two = [finished(&dir, 1, 1), finished(&dir, 2, 1)];
        assert!(up_through(&dir, &two, 1).expect("below").steps.is_empty());
        assert!(up_through(&dir, &two, 2).expect("at").steps.is_empty());
        assert_eq!(keys(&up_through(&dir, &two, 3).expect("above")), [(3, 1)]);
    }

    // -- ahead_only ----------------------------------------------------------------------

    #[test]
    fn records_only_ahead_of_the_directory_say_allow_ahead_would_let_the_run_through() {
        let full = three();
        let records = [
            finished(&full, 1, 1),
            finished(&full, 2, 1),
            finished(&full, 3, 1),
        ];
        let mut behind = full.clone();
        behind.migrations.truncate(2);
        let refusal = up(&behind, &records).expect_err("ahead");
        assert_eq!(refusal.kind(), "applied_missing");
        assert!(refusal.ahead_only());
    }

    #[test]
    fn records_ahead_and_another_refusal_say_allow_ahead_would_not() {
        let full = three();
        let mut records = vec![
            finished(&full, 1, 1),
            finished(&full, 2, 1),
            finished(&full, 3, 1),
        ];
        let mut behind = full.clone();
        behind.migrations.truncate(2);
        // An applied file edited on disk: allow_ahead lets the ahead
        // migration through, and the run still refuses.
        records[0].checksum = OTHER.into();
        let refusal = up(&behind, &records).expect_err("ahead and edited");
        assert_eq!(refusal.kind(), "applied_missing");
        assert!(!refusal.ahead_only());

        // A record the directory lacks below its head is missing, not ahead.
        let mut renamed = finished(&full, 1, 1);
        renamed.migration_name = "0001_other".into();
        assert!(!up(&full, &[renamed]).expect_err("missing").ahead_only());
        // Going down, allow_ahead lets nothing through.
        let down = plan_run(
            &behind,
            &records[1..],
            Dialect::Sqlite,
            Direction::Down {
                target: Target::Latest,
            },
            false,
            None,
        )
        .expect_err("ahead");
        assert!(!down.ahead_only());
    }

    // -- step class and allow_ahead="expand-only" (ADR-0054) ----------------------------

    fn up_allowing(
        dir: &MigrationsDir,
        records: &[StepRecord],
        allow_ahead: AllowAhead,
    ) -> Result<RunPlan, RunRefusal> {
        plan_run(
            dir,
            records,
            Dialect::Sqlite,
            Direction::Up { through: None },
            allow_ahead,
            None,
        )
    }

    #[test]
    fn a_planned_step_records_the_class_its_migration_decides() {
        let dir = as_generated(three());
        let plan = up(&dir, &[]).expect("plan");
        // Each migration adds a table: expand.
        assert!(
            plan.steps
                .iter()
                .all(|step| step.record.class == Some(StepClass::Expand)),
            "{:?}",
            plan.steps
                .iter()
                .map(|s| s.record.class)
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn expand_only_admits_a_database_ahead_only_by_expand_steps() {
        let full = as_generated(three());
        let records = [
            finished(&full, 1, 1),
            finished(&full, 2, 1),
            finished(&full, 3, 1),
        ];
        let mut behind = full.clone();
        behind.migrations.truncate(2);
        let plan = up_allowing(&behind, &records, AllowAhead::ExpandOnly).expect("admitted");
        assert!(plan.steps.is_empty());
        assert_eq!(plan.ahead, ["0003_add_orgs"]);
    }

    #[test]
    fn expand_only_refuses_a_contract_step_and_names_it() {
        let full = three();
        let mut records = vec![
            finished(&full, 1, 1),
            finished(&full, 2, 1),
            finished(&full, 3, 1),
        ];
        let mut behind = full.clone();
        behind.migrations.truncate(2);
        for class in [Some(StepClass::Contract), None] {
            // A record an older ferro wrote has no class: it is contract.
            records[2].class = class;
            let refusal =
                up_allowing(&behind, &records, AllowAhead::ExpandOnly).expect_err("contract");
            assert_eq!(refusal.kind(), "applied_missing");
            assert!(refusal.ahead_only(), "allow_ahead=True would admit it");
            assert_eq!(refusal.contract_steps(), ["0003_add_orgs:01"]);
            let text = refusal.to_string();
            assert!(text.contains("0003_add_orgs:01 (contract"), "{text}");
            assert!(text.contains("expand-only"), "{text}");
            assert!(text.ends_with("Nothing was applied."), "{text}");
            // Without expand-only the text is the plain ahead refusal; the
            // contract steps are still named on the refusal.
            let plain = up(&behind, &records).expect_err("ahead");
            assert_eq!(plain.contract_steps(), ["0003_add_orgs:01"]);
            assert!(!plain.to_string().contains("contract"), "{plain}");
            // `True` admits any distance; `False` refuses any.
            assert!(up_allowing(&behind, &records, AllowAhead::Any).is_ok());
            assert!(up_allowing(&behind, &records, AllowAhead::No).is_err());
        }
    }

    #[test]
    fn records_without_a_class_are_classified_from_the_directory_or_contract() {
        let full = as_generated(three());
        let mut records = vec![
            finished(&full, 1, 1),
            finished(&full, 2, 1),
            finished(&full, 3, 1),
        ];
        for record in &mut records {
            record.class = None;
        }
        assert_eq!(
            unclassified(&full, &records, Dialect::Sqlite).classes,
            [
                (1, 1, StepClass::Expand),
                (2, 1, StepClass::Expand),
                (3, 1, StepClass::Expand),
            ]
        );
        // The migration's files are gone: its records are contract.
        let mut behind = full.clone();
        behind.migrations.truncate(2);
        assert_eq!(
            unclassified(&behind, &records, Dialect::Sqlite).classes,
            [
                (1, 1, StepClass::Expand),
                (2, 1, StepClass::Expand),
                (3, 1, StepClass::Contract),
            ]
        );
        // A classified record is not classified again.
        records[0].class = Some(StepClass::Contract);
        assert_eq!(
            unclassified(&full, &records, Dialect::Sqlite).classes.len(),
            2
        );
    }

    #[test]
    fn a_record_is_classified_from_files_only_when_they_are_the_ones_it_applied() {
        let full = as_generated(three());
        let fresh = || {
            let mut records = vec![
                finished(&full, 1, 1),
                finished(&full, 2, 1),
                finished(&full, 3, 1),
            ];
            for record in &mut records {
                record.class = None;
            }
            records
        };
        // 0002 was replaced in place after the record (`--replace` keeps
        // the number and the name): the snapshot the record names is gone.
        let mut replaced = fresh();
        replaced[1].snapshot_checksum = "b".repeat(96);
        // 0003's step was edited or renamed after it finished.
        replaced[2].checksum = "c".repeat(96);
        assert_eq!(
            unclassified(&full, &replaced, Dialect::Sqlite).classes,
            [
                (1, 1, StepClass::Expand),
                (2, 1, StepClass::Contract),
                (3, 1, StepClass::Contract),
            ]
        );
        let mut renamed = fresh();
        renamed[2].file = "01_old.up.sqlite.sql".into();
        assert_eq!(
            unclassified(&full, &renamed, Dialect::Sqlite).classes[2],
            (3, 1, StepClass::Contract)
        );
    }

    #[test]
    fn a_hand_written_step_is_contract_unless_its_file_says_expand() {
        let hand = |body: &str| {
            let mut files = BTreeMap::new();
            files.insert(
                StepDialect::Portable,
                StepFile {
                    up: PathBuf::from("/m/02_fix.up.sql"),
                    down: Some(PathBuf::from("/m/02_fix.down.sql")),
                    up_checksum: sha384(body.as_bytes()),
                    headers: Headers::parse(body).expect("headers"),
                    down_checksum: Some(sha384(DOWN.as_bytes())),
                    down_headers: Headers::default(),
                },
            );
            Step {
                ordinal: 2,
                name: "fix".into(),
                kind: StepKind::PortableSql,
                files,
            }
        };
        for (body, class) in [
            ("UPDATE a SET id = id;\n", StepClass::Contract),
            (
                "-- ferro: expand\nCREATE INDEX i ON a (id);\n",
                StepClass::Expand,
            ),
        ] {
            let d = as_generated(dir(vec![(
                "create_author",
                vec![ddl(1, "schema", "CREATE TABLE a (id int);\n"), hand(body)],
                &["author"],
            )]));
            let classes = step_class::migration_classes(&d, &d.migrations[0], Dialect::Sqlite)
                .expect("classes");
            assert_eq!(classes[&1], StepClass::Expand);
            assert_eq!(classes[&2], class, "{body}");
        }
    }

    #[test]
    fn status_shows_each_steps_class_a_classless_record_as_contract() {
        let full = as_generated(three());
        let mut first = finished(&full, 1, 1);
        first.class = None;
        let status = run_status(&full, &[first], Dialect::Sqlite, false, None);
        let classes: Vec<StepClass> = status
            .migrations
            .iter()
            .flat_map(|m| m.steps.iter().map(|s| s.class))
            .collect();
        assert_eq!(
            classes,
            [StepClass::Contract, StepClass::Expand, StepClass::Expand]
        );
    }

    // -- what a planned step carries for its execution (ADR-0048) -------------------------

    #[test]
    fn a_step_carries_its_migrations_first_data_step() {
        let dir = backfill();
        let plan = up(&dir, &[]).expect("plan");
        assert!(plan.steps.iter().all(|s| s.first_data_step == Some(2)));
        assert!(
            up(&three(), &[])
                .expect("plan")
                .steps
                .iter()
                .all(|s| s.first_data_step.is_none())
        );
    }

    fn column(name: &str) -> ferro_schema_ir::SchemaColumn {
        serde_json::from_value(serde_json::json!({
            "name": name,
            "logical_type": "integer",
            "nullable": true,
            "primary_key": false,
            "autoincrement": false,
            "unique": false,
            "index": false,
            "default": null,
            "format": null,
        }))
        .expect("column")
    }

    fn model(table: &str, columns: &[&str], renamed_from: Option<&str>) -> SchemaModel {
        SchemaModel {
            renamed_from: renamed_from.map(str::to_string),
            columns: columns.iter().map(|c| column(c)).collect(),
            ..ir(&[table]).payload.models.remove(0)
        }
    }

    fn snapshot(models: Vec<SchemaModel>, parent: Option<[u8; 48]>) -> Snapshot {
        let mut envelope = ir(&[]);
        // Rename hints are schema ir_version 2's.
        envelope.ir_version = 2;
        envelope.payload.models = models;
        let bytes = Snapshot::store(&envelope, parent, &[]).expect("store");
        Snapshot::load(&bytes).expect("load")
    }

    const REBUILD: &str = "-- ferro: foreign-keys-off\n\
        CREATE TABLE \"_ferro_new_author\" (\"id\" integer, \"name\" integer, \"slug\" integer);\n\
        INSERT INTO \"_ferro_new_author\" SELECT \"id\", \"name\", NULL FROM \"author\";\n\
        DROP TABLE \"author\";\n\
        ALTER TABLE \"_ferro_new_author\" RENAME TO \"author\";\n\
        ALTER TABLE \"article\" RENAME TO \"post\";\n\
        CREATE TABLE \"_ferro_new_post\" (\"id\" integer, \"title\" integer);\n\
        INSERT INTO \"_ferro_new_post\" SELECT \"id\", \"title\" FROM \"post\";\n\
        DROP TABLE \"post\";\n\
        ALTER TABLE \"_ferro_new_post\" RENAME TO \"post\";\n";

    /// `0001` holds `author(id, name)` and `article(id, title)`; `0002`'s one
    /// SQLite step rebuilds `author` (adding `slug`) and `post`, which
    /// `0002`'s snapshot renames from `article`.
    fn two_table_rebuild() -> HeldDirectory {
        let first = snapshot(
            vec![
                model("author", &["id", "name"], None),
                model("article", &["id", "title"], None),
            ],
            None,
        );
        let second = snapshot(
            vec![
                model("author", &["id", "name", "slug"], None),
                model("post", &["id", "title"], Some("article")),
            ],
            Some(first.checksum),
        );
        let path = |m: &str, f: &str| PathBuf::from(format!("/proj/migrations/{m}/{f}"));
        let file = |m: &str, name: &str, body: &str, down: &str| StepFile {
            up: path(m, &format!("01_{name}.up.sqlite.sql")),
            down: Some(path(m, &format!("01_{name}.down.sqlite.sql"))),
            up_checksum: sha384(body.as_bytes()),
            headers: Headers::parse(body).expect("headers"),
            down_checksum: Some(sha384(down.as_bytes())),
            down_headers: Headers::parse(down).expect("down headers"),
        };
        let step = |name: &str, file: StepFile| Step {
            ordinal: 1,
            name: name.to_string(),
            kind: StepKind::Ddl,
            files: BTreeMap::from([(StepDialect::Sqlite, file)]),
        };
        let create = "CREATE TABLE author (id int);\n";
        let undo =
            "-- ferro: foreign-keys-off\nCREATE TABLE \"_ferro_new_author\" (\"id\" integer);\n";
        let migrations = vec![
            Migration {
                number: 1,
                name: "init".into(),
                dir: PathBuf::from("/proj/migrations/0001_init"),
                steps: vec![step("schema", file("0001_init", "schema", create, DOWN))],
                witness: Vec::new(),
                snapshot: first,
            },
            Migration {
                number: 2,
                name: "slug".into(),
                dir: PathBuf::from("/proj/migrations/0002_slug"),
                steps: vec![step("rebuild", file("0002_slug", "rebuild", REBUILD, undo))],
                witness: Vec::new(),
                snapshot: second,
            },
        ];
        let dir = MigrationsDir {
            path: PathBuf::from("/proj/migrations"),
            migrations,
        };
        HeldDirectory::from_parts(
            dir,
            [
                (path("0001_init", "01_schema.up.sqlite.sql"), create),
                (path("0001_init", "01_schema.down.sqlite.sql"), DOWN),
                (path("0002_slug", "01_rebuild.up.sqlite.sql"), REBUILD),
                (path("0002_slug", "01_rebuild.down.sqlite.sql"), undo),
            ]
            .map(|(path, text)| (path, text.as_bytes().to_vec())),
        )
        .expect("held")
    }

    fn columns(expectation: &RebuildExpectation) -> Vec<&str> {
        expectation
            .declared
            .columns
            .iter()
            .map(|c| c.name.as_str())
            .collect()
    }

    #[test]
    fn a_two_table_rebuild_step_expects_each_table_as_its_snapshots_declare_it() {
        let held = two_table_rebuild();
        let plan = held
            .plan(
                &[],
                Dialect::Sqlite,
                Direction::Up { through: None },
                false,
                None,
            )
            .expect("plan");
        assert_eq!(keys(&plan), [(1, 1), (2, 1)]);
        assert!(plan.steps[0].rebuilds.is_empty());
        let step = &plan.steps[1];
        assert_eq!(step.mode, ExecMode::ForeignKeysOff);
        let tables: Vec<(&str, &str)> = step
            .rebuilds
            .iter()
            .map(|r| (r.table.as_str(), r.starting_name.as_str()))
            .collect();
        // In the file's order; `post` still has its starting name before
        // the step runs.
        assert_eq!(tables, [("author", "author"), ("post", "article")]);
        // `0001`'s columns, with every column `0002` adds.
        assert_eq!(columns(&step.rebuilds[0]), ["id", "name", "slug"]);
        assert_eq!(step.rebuilds[1].declared.table_name, "article");
        assert_eq!(columns(&step.rebuilds[1]), ["id", "title"]);
        // plan_run sees no bytes, so it reads no rebuild.
        let unheld = plan_run(
            &held.dir,
            &[],
            Dialect::Sqlite,
            Direction::Up { through: None },
            false,
            None,
        )
        .expect("plan");
        assert!(unheld.steps[1].rebuilds.is_empty());
    }

    #[test]
    fn a_reverted_rebuild_starts_from_the_migrations_own_snapshot() {
        let held = two_table_rebuild();
        let records: Vec<StepRecord> = held
            .plan(
                &[],
                Dialect::Sqlite,
                Direction::Up { through: None },
                false,
                None,
            )
            .expect("plan")
            .steps
            .into_iter()
            .map(|s| StepRecord {
                finished_at: Some("2026-10-01T14:02:33.000000Z".into()),
                ..s.record
            })
            .collect();
        let plan = held
            .plan(
                &records,
                Dialect::Sqlite,
                Direction::Down {
                    target: Target::Latest,
                },
                false,
                None,
            )
            .expect("plan");
        let rebuild = &plan.steps[0].rebuilds;
        assert_eq!(rebuild.len(), 1);
        assert_eq!(rebuild[0].table, "author");
        assert_eq!(columns(&rebuild[0]), ["id", "name", "slug"]);
    }

    #[test]
    fn a_rebuilt_table_its_starting_snapshot_lacks_is_refused_before_anything_runs() {
        let mut held = two_table_rebuild();
        held.dir.migrations[0].snapshot = snapshot(vec![model("author", &["id"], None)], None);
        held.dir.migrations[1].snapshot = snapshot(
            vec![
                model("author", &["id", "name", "slug"], None),
                model("post", &["id", "title"], None),
            ],
            Some(held.dir.migrations[0].snapshot.checksum),
        );
        let refusal = held
            .plan(
                &[],
                Dialect::Sqlite,
                Direction::Up { through: None },
                false,
                None,
            )
            .expect_err("undeclared");
        assert_eq!(refusal.kind(), "rebuild");
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: 0002_slug/01_rebuild.up.sqlite.sql rebuilds table \"post\", which \
             the schema snapshot it starts from does not declare. Nothing was applied."
        );
    }

    #[test]
    fn an_edited_unfinished_rebuild_step_expects_what_its_edited_bytes_rebuild() {
        let held = two_table_rebuild();
        let planned = held
            .plan(
                &[],
                Dialect::Sqlite,
                Direction::Up { through: None },
                false,
                None,
            )
            .expect("plan")
            .steps;
        // 0001 applied; 0002's step started under bytes since edited into
        // the held REBUILD (ADR-0030: an unfinished step may be edited).
        let records = [
            StepRecord {
                finished_at: Some("2026-10-01T14:02:33.000000Z".into()),
                ..planned[0].record.clone()
            },
            StepRecord {
                checksum: OTHER.into(),
                started_at: "2026-10-01T14:03:00.000000Z".into(),
                ..planned[1].record.clone()
            },
        ];
        let plan = held
            .plan(
                &records,
                Dialect::Sqlite,
                Direction::Up { through: None },
                false,
                None,
            )
            .expect("plan");
        let step = &plan.steps[0];
        assert_eq!(
            step.edited.as_ref().map(|e| e.recorded.as_str()),
            Some(OTHER)
        );
        let tables: Vec<&str> = step.rebuilds.iter().map(|r| r.table.as_str()).collect();
        assert_eq!(tables, ["author", "post"]);
    }

    #[test]
    fn held_bytes_are_the_hashed_ones() {
        let held = two_table_rebuild();
        let path = PathBuf::from("/proj/migrations/0002_slug/01_rebuild.up.sqlite.sql");
        assert_eq!(held.text(&path), Ok(REBUILD));
        let refusal = HeldDirectory::from_parts(
            held.dir.clone(),
            [(path.clone(), b"DROP TABLE author;\n".to_vec())],
        )
        .expect_err("edited");
        assert_eq!(refusal.kind(), "unreadable");
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: cannot read 0002_slug/01_rebuild.up.sqlite.sql (it changed while the \
             migrations directory was read; run the command again). Nothing was applied."
        );
        let unheld = HeldDirectory::from_parts(held.dir.clone(), []).expect("nothing held");
        assert_eq!(
            unheld
                .plan(
                    &[],
                    Dialect::Sqlite,
                    Direction::Up { through: None },
                    false,
                    None
                )
                .expect_err("no bytes")
                .kind(),
            "unreadable"
        );
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

    const BOTH: [Dialect; 2] = [Dialect::Sqlite, Dialect::Postgres];

    #[test]
    fn the_splitter_keeps_dollar_bodies_quotes_and_comments_whole() {
        let sql = "-- ferro: destructive\n\nDO $$ BEGIN\n  CREATE TYPE \"s\" AS ENUM ('a;b');\n\
                   EXCEPTION WHEN duplicate_object THEN NULL; END $$;\n\n\
                   CREATE TABLE \"t;x\" (\"id\" integer); -- trailing; comment\n\
                   /* block; */ SELECT $tag$ ; $tag$;\nSELECT 'it''s; fine'";
        for dialect in BOTH {
            assert_eq!(
                split_statements(sql, dialect),
                [
                    "-- ferro: destructive\n\nDO $$ BEGIN\n  CREATE TYPE \"s\" AS ENUM ('a;b');\n\
                     EXCEPTION WHEN duplicate_object THEN NULL; END $$",
                    "CREATE TABLE \"t;x\" (\"id\" integer)",
                    "-- trailing; comment\n/* block; */ SELECT $tag$ ; $tag$",
                    "SELECT 'it''s; fine'",
                ]
            );
            assert!(split_statements("-- ferro: not-applicable\n", dialect).is_empty());
            assert!(split_statements("\n  ;\n", dialect).is_empty());
        }
    }

    #[test]
    fn the_splitter_keeps_a_sqlite_trigger_body_whole() {
        let trigger = "CREATE TRIGGER \"t\" AFTER UPDATE ON \"author\" BEGIN\n  \
                       UPDATE \"log\" SET \"n\" = \"n\" + 1;\n  \
                       INSERT INTO \"audit\" (\"what\") VALUES ('end; begin');\nEND";
        assert_eq!(
            split_statements(&format!("{trigger};\nSELECT 1;"), Dialect::Sqlite),
            [trigger, "SELECT 1"]
        );
        let temp = "create temp trigger t2 before delete on author when old.id > 0 begin \
                    delete from log; end";
        assert_eq!(
            split_statements(&format!("{temp};"), Dialect::Sqlite),
            [temp]
        );
        let temporary = "CREATE TEMPORARY TRIGGER IF NOT EXISTS t3 AFTER INSERT ON author \
                         BEGIN SELECT 1; END";
        assert_eq!(
            split_statements(&format!("{temporary};"), Dialect::Sqlite),
            [temporary]
        );
    }

    #[test]
    fn the_splitter_counts_a_case_nested_in_a_trigger_body() {
        let trigger = "CREATE TRIGGER t AFTER UPDATE ON author \
                       WHEN CASE WHEN new.id > 0 THEN 1 ELSE 0 END BEGIN\n  \
                       UPDATE log SET n = CASE WHEN n IS NULL THEN 1 ELSE n + 1 END;\n  \
                       SELECT \"end\", [begin], `case` FROM log /* end; */; -- end;\nEND";
        assert_eq!(
            split_statements(&format!("{trigger};\nDELETE FROM log;"), Dialect::Sqlite),
            [trigger, "DELETE FROM log"]
        );
    }

    #[test]
    fn a_begin_column_in_a_trigger_body_opens_nothing() {
        // `begin` is a legal unquoted SQLite column name; the body holds no
        // nested `BEGIN`, so it is a name and the next statement stands alone.
        let trigger = "CREATE TRIGGER t AFTER UPDATE OF begin ON shift BEGIN\n  \
                       UPDATE shift_log SET begin = new.begin;\n  \
                       UPDATE shift_log SET n = n + 1 WHERE new.end > 0;\nEND";
        let table = "CREATE TABLE \"shift_new\" (\"id\" integer)";
        assert_eq!(
            split_statements(&format!("{trigger};\n{table};"), Dialect::Sqlite),
            [trigger, table]
        );
    }

    #[test]
    fn bracketed_and_non_ascii_words_are_no_keywords() {
        // `[my end]` is a SQLite bracket-quoted identifier; `éend` and
        // `endé` are single identifiers, not the keyword `END`.
        let trigger = "CREATE TRIGGER t AFTER INSERT ON a BEGIN\n  \
                       UPDATE b SET [my end] = 1, éend = 2, endé = 3;\n  \
                       SELECT 1;\nEND";
        assert_eq!(
            split_statements(&format!("{trigger};\nSELECT 2;"), Dialect::Sqlite),
            [trigger, "SELECT 2"]
        );
        let function = "CREATE FUNCTION f() RETURNS void LANGUAGE sql\nBEGIN ATOMIC\n  \
                        UPDATE b SET éend = 2;\n  UPDATE b SET a = 1;\nEND";
        assert_eq!(
            split_statements(&format!("{function};\nSELECT 2;"), Dialect::Postgres),
            [function, "SELECT 2"]
        );
    }

    #[test]
    fn a_trigger_with_no_inner_semicolon_ends_at_its_own() {
        let body = "CREATE TRIGGER t AFTER INSERT ON a BEGIN SELECT 1; END";
        assert_eq!(
            split_statements(&format!("{body};\nSELECT 2;"), Dialect::Sqlite),
            [body, "SELECT 2"]
        );
        // A Postgres trigger has no body: it ends at its `;`.
        let trigger = "CREATE TRIGGER t AFTER UPDATE ON a FOR EACH ROW \
                       WHEN (CASE WHEN new.x THEN true END) EXECUTE FUNCTION f()";
        assert_eq!(
            split_statements(&format!("{trigger};\nSELECT 2;"), Dialect::Postgres),
            [trigger, "SELECT 2"]
        );
    }

    #[test]
    fn the_splitter_keeps_a_postgres_begin_atomic_body_whole() {
        let function = "CREATE FUNCTION bump(a integer) RETURNS integer LANGUAGE sql\n\
                        BEGIN ATOMIC\n  SELECT a + 1;\n  \
                        SELECT CASE WHEN a > 0 THEN a ELSE 0 END;\nEND";
        assert_eq!(
            split_statements(&format!("{function};\nSELECT bump(1);"), Dialect::Postgres),
            [function, "SELECT bump(1)"]
        );
    }

    #[test]
    fn end_if_and_end_loop_in_a_dollar_body_stay_inside_it() {
        let function = "CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$\nBEGIN\n  \
                        IF true THEN\n    LOOP\n      EXIT;\n    END LOOP;\n  END IF;\nEND $$";
        assert_eq!(
            split_statements(&format!("{function};\nSELECT f();"), Dialect::Postgres),
            [function, "SELECT f()"]
        );
    }

    #[test]
    fn the_splitter_still_cuts_a_plain_begin_and_commit() {
        for dialect in BOTH {
            assert_eq!(
                split_statements(
                    "BEGIN;\nUPDATE t SET backend = 1;\nCOMMIT;\n\
                     SELECT CASE WHEN 1 = 1 THEN 'a' END;\nEND;",
                    dialect
                ),
                [
                    "BEGIN",
                    "UPDATE t SET backend = 1",
                    "COMMIT",
                    "SELECT CASE WHEN 1 = 1 THEN 'a' END",
                    "END",
                ]
            );
            // A `trigger` that is not the statement's leading `CREATE …
            // TRIGGER` opens no body.
            assert_eq!(
                split_statements(
                    "DROP TRIGGER IF EXISTS t; SELECT begin_at FROM trigger_log;",
                    dialect
                ),
                [
                    "DROP TRIGGER IF EXISTS t",
                    "SELECT begin_at FROM trigger_log"
                ]
            );
        }
    }

    // A baseline's two refusals a caller branches on (#624, ADR-0031): a
    // tracked database, and a run applied above the baseline. Each carries
    // the migrations it names; the text is the one `ferro migrate baseline`
    // has always printed.

    #[test]
    fn a_tracked_database_refuses_a_baseline_naming_its_records() {
        let refusal = RunRefusal::AlreadyTracked {
            applied: vec!["0001_create_author".into(), "0002_add_teams".into()],
            head: "0002_add_teams".into(),
        };
        assert_eq!(refusal.kind(), "already_tracked");
        assert_eq!(refusal.head(), Some("0002_add_teams"));
        assert_eq!(
            refusal.names(),
            Some(
                &[
                    "0001_create_author".to_string(),
                    "0002_add_teams".to_string()
                ][..]
            )
        );
        assert_eq!(
            refusal.to_string(),
            "ferro migrate baseline: this database already has migration records \
             (0001_create_author, 0002_add_teams); baseline records migrations only on a \
             database that has none. Run `ferro migrate status` to see where it stands. \
             Nothing was recorded."
        );
    }

    #[test]
    fn a_run_above_the_baseline_refuses_its_removal_naming_each() {
        let one = RunRefusal::AppliedAboveBaseline {
            above: vec!["0003_add_orgs".into()],
            floor: "0002_add_teams".into(),
        };
        assert_eq!(one.kind(), "applied_above_baseline");
        assert_eq!(one.names(), Some(&["0003_add_orgs".to_string()][..]));
        assert_eq!(one.head(), None);
        assert_eq!(
            one.to_string(),
            "ferro migrate baseline --remove: 0003_add_orgs was applied by a run above the \
             baseline at 0002_add_teams. Revert it first with `ferro migrate down --to 0002`, \
             then remove the baseline. Nothing was removed."
        );
        let two = RunRefusal::AppliedAboveBaseline {
            above: vec!["0003_add_orgs".into(), "0004_add_squads".into()],
            floor: "0002_add_teams".into(),
        };
        assert_eq!(
            two.to_string(),
            "ferro migrate baseline --remove: 0003_add_orgs, 0004_add_squads were applied by \
             a run above the baseline at 0002_add_teams. Revert them first with `ferro \
             migrate down --to 0002`, then remove the baseline. Nothing was removed."
        );
    }

    #[test]
    fn a_baselines_other_refusals_name_no_migrations() {
        let refusals = [
            RunRefusal::NothingToBaseline {
                directory: "migrations".into(),
            },
            RunRefusal::NoBaselineTarget {
                target: "0009".into(),
                directory: "migrations".into(),
                head: "0002_add_teams".into(),
            },
            RunRefusal::BaselineStep {
                refusal: Box::new(RunRefusal::BadHeaders {
                    file: "0001_a/01_schema.up.sqlite.sql".into(),
                    reason: "declares no-transaction".into(),
                }),
            },
        ];
        assert_eq!(
            refusals.iter().map(RunRefusal::kind).collect::<Vec<_>>(),
            ["nothing_to_baseline", "no_baseline_target", "baseline_step"]
        );
        assert!(
            refusals
                .iter()
                .all(|r| r.names().is_none() && r.head().is_none())
        );
        assert_eq!(
            refusals[0].to_string(),
            "ferro migrate baseline: migrations/ holds no migration to record; generate the \
             first with `ferro migrate new <name>`. Nothing was recorded."
        );
        assert_eq!(
            refusals[1].to_string(),
            "ferro migrate baseline: 0009 names no migration in migrations/: give a migration \
             number from 0001 to 0002, or a migration's full name (0002_add_teams). Nothing \
             was recorded."
        );
        assert_eq!(
            refusals[2].to_string(),
            "ferro migrate baseline: 0001_a/01_schema.up.sqlite.sql: declares no-transaction. \
             Nothing was recorded."
        );
    }

    // -- adoption (ADR-0056) ---------------------------------------------------

    /// `0001_create_author`, then the epic's originals: `0002_add_teams`
    /// (a DDL step and a data step), `0003_team_slug`, `0004_drop_tmp`.
    fn epic_originals() -> MigrationsDir {
        dir(vec![
            (
                "create_author",
                vec![ddl(1, "schema", "CREATE TABLE a (id int);\n")],
                &["author"],
            ),
            (
                "add_teams",
                vec![
                    ddl(1, "expand", "CREATE TABLE t (id int);\n"),
                    data(2, "backfill_team"),
                ],
                &["author", "team"],
            ),
            (
                "team_slug",
                vec![ddl(1, "schema", "ALTER TABLE t ADD slug text;\n")],
                &["author", "team", "slug"],
            ),
            (
                "drop_tmp",
                vec![ddl(1, "schema", "ALTER TABLE t DROP tmp;\n")],
                &["author", "team", "slug", "tmp_gone"],
            ),
        ])
    }

    /// What a squash's witness records of `migration`.
    fn witness_of(migration: &Migration) -> WitnessEntry {
        WitnessEntry {
            migration: migration.dir_name(),
            snapshot_checksum: migration.snapshot.checksum,
            steps: migration.steps.iter().map(|s| s.ordinal).collect(),
            replaces: migration.witness.clone(),
        }
    }

    /// `of` with every migration from `from` on replaced by one squash
    /// named `name`, taking the first's number: its steps an expand, a
    /// carried data step and a contract, its snapshot the last one's
    /// payload chained from the migration below `from`, and its witness
    /// each replaced migration.
    fn squashed(of: &MigrationsDir, from: u16, name: &str) -> MigrationsDir {
        let below: Vec<Migration> = of
            .migrations
            .iter()
            .filter(|m| m.number < from)
            .cloned()
            .collect();
        let witness: Vec<WitnessEntry> = of
            .migrations
            .iter()
            .filter(|m| m.number >= from)
            .map(witness_of)
            .collect();
        let last = of.migrations.last().expect("a head");
        let mut ir = last.snapshot.ir.clone();
        ir.ir_version = 3;
        let parent = below.last().map(|m| m.snapshot.checksum);
        let bytes = Snapshot::store(&ir, parent, &witness).expect("store");
        let mut migrations = below;
        migrations.push(Migration {
            number: from,
            name: name.to_string(),
            dir: PathBuf::from(format!("/migrations/{from:04}_{name}")),
            steps: vec![
                ddl(
                    1,
                    "expand",
                    &format!("-- {name}\nCREATE TABLE t (id int);\n"),
                ),
                data(2, "backfill_team"),
                ddl(
                    3,
                    "contract",
                    &format!("-- {name}\nALTER TABLE t DROP tmp;\n"),
                ),
            ],
            witness,
            snapshot: Snapshot::load(&bytes).expect("load"),
        });
        MigrationsDir {
            path: of.path.clone(),
            migrations,
        }
    }

    /// The finished records `up` writes for every step of `dir`.
    fn every_step(dir: &MigrationsDir) -> Vec<StepRecord> {
        dir.migrations
            .iter()
            .flat_map(|m| m.steps.iter().map(move |s| (m.number, s.ordinal)))
            .map(|(m, s)| finished(dir, m, s))
            .collect()
    }

    /// The squash of `0002..0004`, and a database that applied the originals.
    fn originals_applied() -> (MigrationsDir, Vec<StepRecord>) {
        let originals = epic_originals();
        let records = every_step(&originals);
        (squashed(&originals, 2, "teams_epic"), records)
    }

    fn no_kinds() -> BTreeMap<(u16, u8), RecordKind> {
        BTreeMap::new()
    }

    #[test]
    fn a_squash_whose_witness_the_records_hold_exactly_is_adoptable() {
        let (dir, records) = originals_applied();
        assert_eq!(
            adoptable(&dir, &records),
            [Adoption {
                squash: "0002_teams_epic".into(),
                replaced: vec![
                    "0002_add_teams".into(),
                    "0003_team_slug".into(),
                    "0004_drop_tmp".into()
                ],
                witnessed: vec![(2, 1), (2, 2), (3, 1), (4, 1)],
            }]
        );
        assert!(partial_witnesses(&dir, &records).is_empty());
    }

    #[test]
    fn a_squash_none_of_whose_witness_is_recorded_is_neither_adoptable_nor_partial() {
        let (dir, records) = originals_applied();
        let only_first: Vec<StepRecord> =
            records.into_iter().filter(|r| r.migration == 1).collect();
        assert!(adoptable(&dir, &only_first).is_empty());
        assert!(partial_witnesses(&dir, &only_first).is_empty());
        // Nor is a database that ran the squash itself.
        let ran = every_step(&dir);
        assert!(adoptable(&dir, &ran).is_empty());
        assert!(partial_witnesses(&dir, &ran).is_empty());
    }

    #[test]
    fn an_extra_record_of_a_witnessed_name_is_no_witness() {
        let (dir, mut records) = originals_applied();
        let mut extra = records[2].clone();
        extra.step = 3;
        records.push(extra);
        assert!(adoptable(&dir, &records).is_empty());
        let [partial] = &partial_witnesses(&dir, &records)[..] else {
            panic!("one partial witness");
        };
        assert_eq!(partial.missing, ["0002_add_teams"]);
    }

    #[test]
    fn a_missing_ordinal_is_no_witness() {
        let (dir, records) = originals_applied();
        let records: Vec<StepRecord> = records
            .into_iter()
            .filter(|r| (r.migration, r.step) != (2, 2))
            .collect();
        assert!(adoptable(&dir, &records).is_empty());
        assert_eq!(
            partial_witnesses(&dir, &records)[0].missing,
            ["0002_add_teams"]
        );
    }

    #[test]
    fn a_record_under_another_snapshot_checksum_is_no_witness() {
        let (dir, mut records) = originals_applied();
        records[3].snapshot_checksum = "0".repeat(96);
        assert!(adoptable(&dir, &records).is_empty());
        assert_eq!(
            partial_witnesses(&dir, &records)[0].missing,
            ["0003_team_slug"]
        );
    }

    #[test]
    fn baseline_records_witness_the_squash() {
        let (dir, mut records) = originals_applied();
        for record in &mut records {
            record.origin = Origin::Baseline;
        }
        assert_eq!(adoptable(&dir, &records)[0].squash, "0002_teams_epic");
    }

    #[test]
    fn a_record_whose_up_file_changed_after_apply_still_witnesses() {
        let (dir, mut records) = originals_applied();
        records[3].checksum = "f".repeat(96);
        assert_eq!(adoptable(&dir, &records)[0].squash, "0002_teams_epic");
    }

    #[test]
    fn a_partial_witness_names_what_is_satisfied_and_what_is_missing() {
        let (dir, records) = originals_applied();
        // 0002 and 0003 applied, 0004 never.
        let partial: Vec<StepRecord> = records
            .iter()
            .filter(|r| r.migration < 4)
            .cloned()
            .collect();
        assert!(adoptable(&dir, &partial).is_empty());
        assert_eq!(
            partial_witnesses(&dir, &partial),
            [PartialWitness {
                squash: "0002_teams_epic".into(),
                parent: "0001_create_author".into(),
                satisfied: vec!["0002_add_teams".into(), "0003_team_slug".into()],
                missing: vec!["0004_drop_tmp".into()],
            }]
        );
        // A last step that failed is missing too.
        let mut failed = records;
        failed[4].finished_at = None;
        failed[4].error = Some("boom".into());
        assert!(adoptable(&dir, &failed).is_empty());
        assert_eq!(
            partial_witnesses(&dir, &failed)[0].missing,
            ["0004_drop_tmp"]
        );
    }

    #[test]
    fn a_squash_of_a_squash_adopts_from_the_inner_squash_and_from_the_earliest_originals() {
        let originals = epic_originals();
        // The first squash took 0003..0004; the second takes 0002 and it.
        let first = squashed(&originals, 3, "slug_epic");
        let second = squashed(&first, 2, "big_epic");
        assert_eq!(second.migrations[1].witness[1].replaces.len(), 2);

        // A database that adopted the first squash.
        let adopted: Vec<StepRecord> = every_step(&first)
            .into_iter()
            .map(|r| StepRecord {
                origin: if r.migration == 3 {
                    Origin::Adopted
                } else {
                    r.origin
                },
                ..r
            })
            .collect();
        let [by_inner] = &adoptable(&second, &adopted)[..] else {
            panic!("adoptable from the first squash's records");
        };
        assert_eq!(by_inner.squash, "0002_big_epic");
        assert_eq!(by_inner.replaced, ["0002_add_teams", "0003_slug_epic"]);
        assert_eq!(by_inner.witnessed, [(2, 1), (2, 2), (3, 1), (3, 2), (3, 3)]);

        // A database that still holds the earliest originals.
        let earliest = every_step(&originals);
        let [by_originals] = &adoptable(&second, &earliest)[..] else {
            panic!("adoptable from the earliest originals");
        };
        assert_eq!(
            by_originals.replaced,
            ["0002_add_teams", "0003_team_slug", "0004_drop_tmp"]
        );
        assert_eq!(by_originals.witnessed, [(2, 1), (2, 2), (3, 1), (4, 1)]);

        // Half of the inner squash's originals is a partial witness, named
        // by the originals the database holds.
        let half: Vec<StepRecord> = earliest.into_iter().filter(|r| r.migration != 4).collect();
        assert!(adoptable(&second, &half).is_empty());
        let [partial] = &partial_witnesses(&second, &half)[..] else {
            panic!("partial");
        };
        assert_eq!(partial.satisfied, ["0002_add_teams", "0003_team_slug"]);
        assert_eq!(partial.missing, ["0004_drop_tmp"]);
    }

    #[test]
    fn adoption_swaps_the_witnessed_records_for_the_squashs_own_and_then_nothing_is_pending() {
        let (dir, records) = originals_applied();
        let kinds = BTreeMap::from([((2, 2), RecordKind::Chunked)]);
        let adopted = adoption(
            &dir,
            &records,
            Dialect::Sqlite,
            &kinds,
            "2026-10-18T12:00:00.000000Z",
            "0.25.0",
        )
        .expect("adopts");
        assert_eq!(adopted.adoptions.len(), 1);
        assert_eq!(adopted.removed, [(2, 1), (2, 2), (3, 1), (4, 1)]);
        let squash = &dir.migrations[1];
        let snapshot = encode_checksum(&squash.snapshot.checksum);
        let classes = migration_classes(&dir, squash, Dialect::Sqlite).expect("classes");
        assert_eq!(adopted.inserted.len(), 3);
        for (record, step) in adopted.inserted.iter().zip(&squash.steps) {
            let file = step_file(squash, step, Dialect::Sqlite).expect("file");
            assert_eq!(record.migration_name, "0002_teams_epic");
            assert_eq!((record.migration, record.step), (2, step.ordinal));
            assert_eq!(record.file, file_name(&file.up));
            assert_eq!(record.checksum, encode_checksum(&file.up_checksum));
            assert_eq!(record.snapshot_checksum, snapshot);
            assert_eq!(record.origin, Origin::Adopted);
            assert_eq!(record.class, classes.get(&step.ordinal).copied());
            assert_eq!(record.started_at, "2026-10-18T12:00:00.000000Z");
            assert_eq!(
                record.finished_at.as_deref(),
                Some("2026-10-18T12:00:00.000000Z")
            );
            assert_eq!(record.ferro_version, "0.25.0");
            assert_eq!(record.duration_ms, 0);
        }
        assert_eq!(adopted.inserted[0].kind, RecordKind::Ddl);
        assert_eq!(adopted.inserted[1].kind, RecordKind::Chunked);
        // The records after: 0001's, then the squash's.
        assert_eq!(
            adopted
                .records
                .iter()
                .map(|r| (r.migration_name.as_str(), r.step))
                .collect::<Vec<_>>(),
            [
                ("0001_create_author", 1),
                ("0002_teams_epic", 1),
                ("0002_teams_epic", 2),
                ("0002_teams_epic", 3)
            ]
        );
        assert!(up(&dir, &adopted.records).expect("plans").steps.is_empty());
        // Adopting again finds nothing.
        let again = adoption(&dir, &adopted.records, Dialect::Sqlite, &kinds, "now", "v")
            .expect("nothing to adopt");
        assert!(again.adoptions.is_empty() && again.inserted.is_empty());
        assert_eq!(again.records, adopted.records);
        // Its data steps are the files whose declared kind the caller reads.
        assert_eq!(
            adoption_data_steps(&dir, &records),
            [(2, 2, PathBuf::from("/m/02_backfill_team.py"))]
        );
        assert!(adoption_data_steps(&dir, &adopted.records).is_empty());
    }

    #[test]
    fn a_partial_witness_refuses_the_adoption_naming_the_non_destructive_fix_first() {
        let (dir, records) = originals_applied();
        let partial: Vec<StepRecord> = records.into_iter().filter(|r| r.migration < 4).collect();
        let refusal = adoption(&dir, &partial, Dialect::Sqlite, &no_kinds(), "now", "v")
            .expect_err("partial");
        assert_eq!(refusal.kind(), "partial_witness");
        assert_eq!(
            refusal,
            RunRefusal::PartialWitness {
                squash: "0002_teams_epic".into(),
                parent: "0001_create_author".into(),
                satisfied: vec!["0002_add_teams".into(), "0003_team_slug".into()],
                missing: vec!["0004_drop_tmp".into()],
            }
        );
        assert_eq!(
            refusal.to_string(),
            "ferro migrate: 0002_teams_epic replaces migrations this database applied only \
             part of: it applied 0002_add_teams and 0003_team_slug but not 0004_drop_tmp, so \
             it cannot adopt 0002_teams_epic. To keep its rows, check out the commit that \
             holds 0004_drop_tmp, run `ferro migrate up` there to finish it, then run `ferro \
             migrate up` here, which adopts 0002_teams_epic. Or revert instead: on the commit \
             that applied them run `ferro migrate down --to 0001`, then `ferro migrate up` \
             here; the revert drops what they created and fails on an irreversible step. \
             Nothing was changed."
        );
    }

    #[test]
    fn an_adopted_step_reads_applied_adopted_and_its_down_runs() {
        assert_eq!(Origin::parse("adopted"), Some(Origin::Adopted));
        assert_eq!(Origin::Adopted.as_str(), "adopted");
        assert!(!Origin::Adopted.is_baseline() && !Origin::Run.is_baseline());
        assert!(Origin::Baseline.is_baseline());
        let (dir, records) = originals_applied();
        let adopted =
            adoption(&dir, &records, Dialect::Sqlite, &no_kinds(), "now", "v").expect("adopts");
        let status = run_status(&dir, &adopted.records, Dialect::Sqlite, false, None);
        assert_eq!(
            status.migrations[1]
                .steps
                .iter()
                .map(|s| s.state)
                .collect::<Vec<_>>(),
            [StepState::AppliedAdopted; 3]
        );
        assert_eq!(status.migrations[0].steps[0].state, StepState::Applied);
        assert!(!StepState::AppliedAdopted.is_pending());
        assert!(!StepState::AppliedAdopted.needs_attention());
        assert_eq!(
            serde_json::to_value(StepState::AppliedAdopted).expect("json"),
            "applied_adopted"
        );
        assert!(status.refusal.is_none(), "{:?}", status.refusal);
        // An adopted step's down runs: the floor is a baseline's alone.
        let reverted = plan_run(
            &dir,
            &adopted.records,
            Dialect::Sqlite,
            Direction::Down {
                target: Target::Migration(1),
            },
            false,
            None,
        )
        .expect("down");
        assert_eq!(keys(&reverted), [(2, 3), (2, 2), (2, 1)]);
    }

    // -- the readers on an adoptable database (#672) ----------------------------

    fn states(status: &RunStatus, migration: usize) -> Vec<StepState> {
        status.migrations[migration]
            .steps
            .iter()
            .map(|s| s.state)
            .collect()
    }

    #[test]
    fn status_reports_a_wholly_witnessed_squash_pending_adoptable_and_nothing_ahead() {
        let (dir, records) = originals_applied();
        let status = run_status(&dir, &records, Dialect::Sqlite, false, None);
        assert_eq!(states(&status, 0), [StepState::Applied]);
        assert_eq!(states(&status, 1), [StepState::PendingAdoptable; 3]);
        assert_eq!(status.adoptable, ["0002_teams_epic"]);
        assert_eq!(
            status.migrations[1].adopts,
            ["0002_add_teams", "0003_team_slug", "0004_drop_tmp"]
        );
        assert!(status.migrations[0].adopts.is_empty());
        assert!(status.ahead.is_empty(), "{:?}", status.ahead);
        // The run adopts and then has nothing to refuse.
        assert!(status.refusal.is_none(), "{:?}", status.refusal);
        assert!(StepState::PendingAdoptable.is_pending());
        assert!(!StepState::PendingAdoptable.needs_attention());
        assert_eq!(
            serde_json::to_value(StepState::PendingAdoptable).expect("json"),
            "pending_adoptable"
        );
        // A squash step's class is the one a run would record, never an
        // original's record of the same number and ordinal.
        let classes =
            migration_classes(&dir, &dir.migrations[1], Dialect::Sqlite).expect("classes");
        for step in &status.migrations[1].steps {
            assert_eq!(Some(step.class), classes.get(&step.step).copied());
            assert_eq!(step.applied_checksum, None);
        }
    }

    #[test]
    fn status_reports_a_partial_witness_ahead_by_the_originals_it_holds_with_the_runs_refusal() {
        let (dir, records) = originals_applied();
        let partial: Vec<StepRecord> = records.into_iter().filter(|r| r.migration < 4).collect();
        let status = run_status(&dir, &partial, Dialect::Sqlite, false, None);
        assert_eq!(states(&status, 1), [StepState::Pending; 3]);
        assert!(status.adoptable.is_empty());
        assert!(status.migrations[1].adopts.is_empty());
        assert_eq!(status.ahead, ["0002_add_teams", "0003_team_slug"]);
        let refusal = adoption(&dir, &partial, Dialect::Sqlite, &no_kinds(), "now", "v")
            .expect_err("partial");
        assert_eq!(status.refusal, Some(refusal.to_string()));
        assert!(status.refusal_needs_attention);
    }

    #[test]
    fn status_reports_a_squash_of_a_squash_adoptable_from_either_generation() {
        let originals = epic_originals();
        let first = squashed(&originals, 3, "slug_epic");
        let second = squashed(&first, 2, "big_epic");
        let inner: Vec<StepRecord> = every_step(&first)
            .into_iter()
            .map(|r| StepRecord {
                origin: if r.migration == 3 {
                    Origin::Adopted
                } else {
                    r.origin
                },
                ..r
            })
            .collect();
        for (records, adopts) in [
            (inner, vec!["0002_add_teams", "0003_slug_epic"]),
            (
                every_step(&originals),
                vec!["0002_add_teams", "0003_team_slug", "0004_drop_tmp"],
            ),
        ] {
            let status = run_status(&second, &records, Dialect::Sqlite, false, None);
            assert_eq!(states(&status, 1), [StepState::PendingAdoptable; 3]);
            assert_eq!(status.adoptable, ["0002_big_epic"]);
            assert_eq!(status.migrations[1].adopts, adopts);
            assert!(status.ahead.is_empty(), "{:?}", status.ahead);
            assert!(status.refusal.is_none(), "{:?}", status.refusal);
        }
    }

    #[test]
    fn status_after_adoption_reads_applied_adopted_with_nothing_adoptable() {
        let (dir, records) = originals_applied();
        let adopted =
            adoption(&dir, &records, Dialect::Sqlite, &no_kinds(), "now", "v").expect("adopts");
        let status = run_status(&dir, &adopted.records, Dialect::Sqlite, false, None);
        assert_eq!(states(&status, 1), [StepState::AppliedAdopted; 3]);
        assert!(status.adoptable.is_empty());
        assert!(status.ahead.is_empty());
    }
}
