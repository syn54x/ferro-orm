//! Reading a migrations directory (ADR-0026, ADR-0037): the numbered
//! migrations, their steps, each step's files per dialect, the leading
//! `-- ferro: …` headers, the snapshot chain — and every refusal a malformed
//! directory earns, each naming the files involved and the fix.
//!
//! ```text
//! migrations/
//!   .gitattributes                       ignored (dot-entries and _-entries are not migrations)
//!   0001_create_author/
//!     01_schema.up.postgres.sql          a DDL step: one rendering per target dialect
//!     01_schema.down.postgres.sql
//!     02_fix_rows.up.sql                 a hand-written portable step: serves every dialect
//!     02_fix_rows.down.sql
//!     03_backfill_author.py              a data step
//!     ir.json                            the schema snapshot
//! ```

use crate::Dialect;
use crate::plan::and_list;
use crate::snapshot::{Snapshot, SnapshotError, encode_checksum, sha384};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// The snapshot file inside every migration.
pub const SNAPSHOT_FILE: &str = "ir.json";

/// Which dialect a step file serves: one target dialect, or every dialect (an
/// unsuffixed hand-written `.sql` file, or a data step).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum StepDialect {
    /// `.postgres.sql`.
    Postgres,
    /// `.sqlite.sql`.
    Sqlite,
    /// Unsuffixed: serves every dialect.
    Portable,
}

impl StepDialect {
    /// The file-name suffix of a target dialect's rendering.
    pub fn suffix(self) -> Option<&'static str> {
        match self {
            StepDialect::Postgres => Some("postgres"),
            StepDialect::Sqlite => Some("sqlite"),
            StepDialect::Portable => None,
        }
    }

    fn from_suffix(suffix: &str) -> Option<Self> {
        match suffix {
            "postgres" => Some(StepDialect::Postgres),
            "sqlite" => Some(StepDialect::Sqlite),
            _ => None,
        }
    }
}

impl From<Dialect> for StepDialect {
    fn from(dialect: Dialect) -> Self {
        match dialect {
            Dialect::Postgres => StepDialect::Postgres,
            Dialect::Sqlite => StepDialect::Sqlite,
        }
    }
}

/// What a step is.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StepKind {
    /// Generated SQL, one rendering per target dialect.
    Ddl,
    /// A Python data step (`NN_<name>.py`).
    Data,
    /// Hand-written SQL that serves every dialect (`NN_<name>.up.sql`).
    PortableSql,
}

/// The leading `-- ferro: …` lines of a step file (ADR-0024, ADR-0037).
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct Headers {
    /// `-- ferro: no-transaction`
    pub no_transaction: bool,
    /// `-- ferro: foreign-keys-off`
    pub foreign_keys_off: bool,
    /// `-- ferro: destructive`
    pub destructive: bool,
    /// `-- ferro: data-dependent`
    pub data_dependent: bool,
    /// `-- ferro: not-applicable`
    pub not_applicable: bool,
    /// `-- ferro: expand`: a hand-written SQL step its author declares
    /// `expand` (ADR-0054); without it a hand-written step is `contract`. The
    /// generator never writes it: a generated step's class is the planner's.
    /// A ferro older than the header refuses the file, as it refuses any
    /// header it does not know.
    pub expand: bool,
    /// `-- ferro: nothing-to-reverse <reason>`
    pub nothing_to_reverse: Option<String>,
    /// `-- ferro: irreversible <reason>`
    pub irreversible: Option<String>,
}

/// Every header line opens with this.
pub const HEADER_PREFIX: &str = "-- ferro:";

impl Headers {
    /// Read the headers from a step file's text: every leading line that
    /// opens with `-- ferro:`. Reading stops at the first other line. A
    /// declaration's reason follows its token after a space (ADR-0033:
    /// `-- ferro: irreversible dropped rows cannot come back`).
    ///
    /// # Errors
    /// The offending line, when a header line names no known header, a flag
    /// carries text after it, or a declaration has no reason.
    pub fn parse(text: &str) -> Result<Headers, String> {
        let mut headers = Headers::default();
        for line in text.lines() {
            let Some(rest) = line.strip_prefix(HEADER_PREFIX) else {
                break;
            };
            let rest = rest.trim();
            let (token, reason) = match rest.split_once(char::is_whitespace) {
                Some((token, reason)) => (token, Some(reason.trim().to_string())),
                None => (rest, None),
            };
            match (token, reason) {
                ("no-transaction", None) => headers.no_transaction = true,
                ("foreign-keys-off", None) => headers.foreign_keys_off = true,
                ("destructive", None) => headers.destructive = true,
                ("data-dependent", None) => headers.data_dependent = true,
                ("not-applicable", None) => headers.not_applicable = true,
                ("expand", None) => headers.expand = true,
                ("nothing-to-reverse", Some(reason)) if !reason.is_empty() => {
                    headers.nothing_to_reverse = Some(reason)
                }
                ("irreversible", Some(reason)) if !reason.is_empty() => {
                    headers.irreversible = Some(reason)
                }
                _ => return Err(line.to_string()),
            }
        }
        Ok(headers)
    }

    /// The header lines, in a fixed order, each ending in a newline.
    pub fn render(&self) -> String {
        let mut lines = Vec::new();
        let flags = [
            (self.not_applicable, "not-applicable"),
            (self.no_transaction, "no-transaction"),
            (self.foreign_keys_off, "foreign-keys-off"),
            (self.destructive, "destructive"),
            (self.data_dependent, "data-dependent"),
            (self.expand, "expand"),
        ];
        for (set, token) in flags {
            if set {
                lines.push(format!("{HEADER_PREFIX} {token}\n"));
            }
        }
        if let Some(reason) = &self.nothing_to_reverse {
            lines.push(format!("{HEADER_PREFIX} nothing-to-reverse {reason}\n"));
        }
        if let Some(reason) = &self.irreversible {
            lines.push(format!("{HEADER_PREFIX} irreversible {reason}\n"));
        }
        lines.concat()
    }
}

/// One dialect's files of a step.
#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct StepFile {
    /// The up file.
    pub up: PathBuf,
    /// The down file (`None` for a data step, whose down lives in the same file).
    pub down: Option<PathBuf>,
    /// SHA-384 of the up file's raw bytes.
    #[serde(serialize_with = "serialize_checksum")]
    pub up_checksum: [u8; 48],
    /// The up file's headers.
    pub headers: Headers,
    /// SHA-384 of the down file's raw bytes (`None` for a data step).
    #[serde(serialize_with = "serialize_optional_checksum")]
    pub down_checksum: Option<[u8; 48]>,
    /// The down file's headers: what a run reverting the step reads, before
    /// it reverts anything (ADR-0033).
    pub down_headers: Headers,
}

/// One step of a migration.
#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct Step {
    /// `NN`, from 1.
    pub ordinal: u8,
    /// The name after `NN_`.
    pub name: String,
    /// What the step is.
    pub kind: StepKind,
    /// Its files by the dialect they serve.
    pub files: BTreeMap<StepDialect, StepFile>,
}

/// One migration directory.
#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct Migration {
    /// `NNNN`, from 1.
    pub number: u16,
    /// The name after `NNNN_`.
    pub name: String,
    /// The directory.
    pub dir: PathBuf,
    /// Its steps, by ordinal.
    pub steps: Vec<Step>,
    /// Its `ir.json`.
    #[serde(serialize_with = "serialize_snapshot")]
    pub snapshot: Snapshot,
}

impl Migration {
    /// `NNNN_<name>`.
    pub fn dir_name(&self) -> String {
        format!("{:04}_{}", self.number, self.name)
    }
}

/// A whole migrations directory, read and verified.
#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct MigrationsDir {
    /// The directory.
    pub path: PathBuf,
    /// Its migrations in number order; dense from `0001`, each snapshot
    /// linked to the one before by checksum.
    pub migrations: Vec<Migration>,
}

/// Why a migrations directory cannot be used. Every path is relative to the
/// migrations directory.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DirectoryError {
    /// A file or directory could not be read.
    Unreadable {
        /// What could not be read.
        path: PathBuf,
        /// The operating system's reason.
        reason: String,
    },
    /// An entry that is neither a migration nor a step nor ignored.
    UnexpectedEntry {
        /// The entry.
        path: PathBuf,
        /// The names it should have.
        expected: &'static str,
    },
    /// Two migrations share a number.
    DuplicateNumber {
        /// The shared number.
        number: u16,
        /// One migration.
        first: PathBuf,
        /// The other.
        second: PathBuf,
    },
    /// `ferro migrate new --replace NNNN` names a number two migrations
    /// share.
    AmbiguousNumber {
        /// The shared number.
        number: u16,
        /// Every migration carrying it (`NNNN_<name>`), in name order.
        names: Vec<String>,
    },
    /// `ferro migrate new --replace` names no migration of the directory.
    NoSuchMigration {
        /// The target as written.
        target: String,
    },
    /// `ferro migrate new --replace` names a migration a later one chains
    /// from: only the newest migration can be replaced.
    LaterMigration {
        /// The migration named (`NNNN_<name>`).
        replaced: String,
        /// The later migration whose snapshot links to it (`NNNN_<name>`).
        later: String,
    },
    /// A number below the highest is missing.
    MissingNumber {
        /// The missing number.
        number: u16,
        /// The migration after the gap.
        next: PathBuf,
    },
    /// A migration without `ir.json`.
    MissingSnapshot {
        /// The migration.
        migration: PathBuf,
    },
    /// An `ir.json` that does not load.
    UnreadableSnapshot {
        /// The file.
        path: PathBuf,
        /// Why.
        error: SnapshotError,
    },
    /// A snapshot whose `parent_checksum` is not its predecessor's checksum.
    BrokenChain {
        /// The previous migration's `ir.json` (`None` when the child is `0001`).
        parent: Option<PathBuf>,
        /// The `ir.json` whose link is wrong.
        child: PathBuf,
    },
    /// Two steps share an ordinal.
    DuplicateStep {
        /// One file.
        first: PathBuf,
        /// The other.
        second: PathBuf,
    },
    /// A step number below the highest is missing (or a migration has none).
    MissingStep {
        /// The migration.
        migration: PathBuf,
        /// The missing ordinal.
        ordinal: u8,
    },
    /// A step with both an unsuffixed and a dialect-suffixed file.
    BothSuffixedAndUnsuffixed {
        /// The suffixed file.
        suffixed: PathBuf,
        /// The unsuffixed file.
        unsuffixed: PathBuf,
    },
    /// An up file without its down, or a down without its up.
    MissingPair {
        /// The file present.
        present: PathBuf,
        /// The file missing.
        missing: PathBuf,
    },
    /// A `-- ferro:` header line that names no header.
    UnparseableHeader {
        /// The file.
        file: PathBuf,
        /// The line.
        line: String,
    },
    /// A migration `ferro migrate new --replace` set aside under a hidden
    /// name (`.NNNN_<name>.replaced`, or `.NNNN_<name>.MMMM_<new>.replaced`
    /// when the replacement takes another name) and never deleted: the run
    /// was killed between setting it aside and deleting it.
    InterruptedReplace {
        /// The hidden directory.
        aside: PathBuf,
        /// The migration it holds (`NNNN_<name>`).
        replaced: String,
        /// The replacement's directory name (`MMMM_<new>`).
        replacement: String,
        /// Whether the replacement is in the directory: it appears whole,
        /// by one rename, so present means complete.
        complete: bool,
    },
    /// A migration `ferro migrate new` was writing into a hidden sibling
    /// (`.NNNN_<name>.partial`) and never moved into place: the run was
    /// killed while writing it.
    InterruptedWrite {
        /// The hidden directory.
        partial: PathBuf,
        /// The migration it was becoming (`NNNN_<name>`).
        migration: String,
    },
    /// A DDL step with no rendering for a target dialect.
    MissingRendering {
        /// The migration.
        migration: PathBuf,
        /// The step (`NN_<name>`).
        step: String,
        /// The target dialect.
        dialect: StepDialect,
    },
}

impl DirectoryError {
    /// A stable identifier for the kind of problem, used by reports.
    pub fn kind(&self) -> &'static str {
        match self {
            DirectoryError::Unreadable { .. } => "unreadable",
            DirectoryError::UnexpectedEntry { .. } => "unexpected_entry",
            DirectoryError::DuplicateNumber { .. } => "duplicate_number",
            DirectoryError::AmbiguousNumber { .. } => "ambiguous_number",
            DirectoryError::NoSuchMigration { .. } => "no_such_migration",
            DirectoryError::LaterMigration { .. } => "later_migration",
            DirectoryError::MissingNumber { .. } => "missing_number",
            DirectoryError::MissingSnapshot { .. } => "missing_snapshot",
            DirectoryError::UnreadableSnapshot { .. } => "unreadable_snapshot",
            DirectoryError::BrokenChain { .. } => "broken_chain",
            DirectoryError::DuplicateStep { .. } => "duplicate_step",
            DirectoryError::MissingStep { .. } => "missing_step",
            DirectoryError::BothSuffixedAndUnsuffixed { .. } => "suffixed_and_unsuffixed",
            DirectoryError::MissingPair { .. } => "missing_pair",
            DirectoryError::UnparseableHeader { .. } => "unparseable_header",
            DirectoryError::MissingRendering { .. } => "missing_rendering",
            DirectoryError::InterruptedReplace { .. } => "interrupted_replace",
            DirectoryError::InterruptedWrite { .. } => "interrupted_write",
        }
    }
}

impl std::fmt::Display for DirectoryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let show = |path: &Path| path.display().to_string();
        match self {
            DirectoryError::Unreadable { path, reason } => {
                write!(f, "cannot read {}: {reason}", show(path))
            }
            DirectoryError::UnexpectedEntry { path, expected } => write!(
                f,
                "{} is not part of a migration; expected {expected}. Move it out of the \
                 migrations directory (names starting with '.' or '_' are ignored)",
                show(path)
            ),
            DirectoryError::DuplicateNumber {
                number,
                first,
                second,
            } => {
                write!(
                    f,
                    "two migrations are numbered {number:04}: {} and {}. Keep the one that is \
                     applied anywhere and regenerate the other on top of it:",
                    show(first),
                    show(second)
                )?;
                for dir in [first, second] {
                    write!(f, "\n  {}", replace_line(dir))?;
                }
                write!(f, "\n(each line regenerates the migration it names)")
            }
            DirectoryError::AmbiguousNumber { number, names } => {
                let named: Vec<String> = names.iter().map(|n| format!("--replace {n}")).collect();
                write!(
                    f,
                    "--replace {number:04} matches {}; name the migration to replace in \
                     full: {}",
                    and_list(names),
                    or_list(&named)
                )
            }
            DirectoryError::NoSuchMigration { target } => write!(
                f,
                "--replace {target}: the migrations directory holds no migration {target}; \
                 name one of its migrations as NNNN_<name>, or as NNNN when one migration \
                 carries the number"
            ),
            DirectoryError::LaterMigration { replaced, later } => write!(
                f,
                "{later} chains from {replaced}, and only the newest migration can be \
                 replaced: replace the later one first, or delete it. Nothing was written"
            ),
            DirectoryError::MissingNumber { number, next } => write!(
                f,
                "migration {number:04} is missing: {} follows a gap. Numbers are dense, so a \
                 missing number is a lost directory; restore it from version control",
                show(next)
            ),
            DirectoryError::MissingSnapshot { migration } => write!(
                f,
                "{} has no {SNAPSHOT_FILE}; restore it from version control",
                show(migration)
            ),
            DirectoryError::UnreadableSnapshot { path, error } => {
                write!(f, "{} {error}; restore it from version control", show(path))
            }
            DirectoryError::BrokenChain {
                parent: Some(parent),
                child,
            } => write!(
                f,
                "broken chain: the parent_checksum in {} is not the SHA-384 of {}. One of \
                 them was edited after it was generated; a snapshot is never re-recorded, so \
                 restore the edited file from version control",
                show(child),
                show(parent)
            ),
            DirectoryError::BrokenChain {
                parent: None,
                child,
            } => write!(
                f,
                "broken chain: {} is the first migration but its parent_checksum is not null; \
                 restore it from version control",
                show(child)
            ),
            DirectoryError::DuplicateStep { first, second } => write!(
                f,
                "two steps share a step number: {} and {}; renumber one so step numbers \
                 are dense and unique",
                show(first),
                show(second)
            ),
            DirectoryError::MissingStep { migration, ordinal } => write!(
                f,
                "{} has no step {ordinal:02}; step numbers are dense from 01, so a missing \
                 number is a lost file; restore it from version control",
                show(migration)
            ),
            DirectoryError::BothSuffixedAndUnsuffixed {
                suffixed,
                unsuffixed,
            } => write!(
                f,
                "{} is a portable step serving every dialect, but {} is a rendering of the \
                 same step for one dialect; keep one of the two",
                show(unsuffixed),
                show(suffixed)
            ),
            DirectoryError::MissingPair { present, missing } => write!(
                f,
                "{} has no {}; every SQL step has an up and a down",
                show(present),
                show(missing)
            ),
            DirectoryError::UnparseableHeader { file, line } => write!(
                f,
                "{}: {line:?} is not a ferro header; the headers are no-transaction, \
                 foreign-keys-off, destructive, data-dependent, not-applicable, expand, \
                 nothing-to-reverse <reason> and irreversible <reason>",
                show(file)
            ),
            DirectoryError::InterruptedReplace {
                aside,
                replaced,
                replacement,
                complete,
            } => {
                write!(
                    f,
                    "{} is {replaced}, set aside by an interrupted `ferro migrate new \
                     --replace {replaced}`; ",
                    show(aside)
                )?;
                if *complete {
                    write!(
                        f,
                        "its replacement {replacement} is complete. Delete {}",
                        show(aside)
                    )
                } else {
                    let name = split_numbered(replacement, 4)
                        .map_or_else(|| replacement.clone(), |(_, name)| name);
                    write!(
                        f,
                        "{replacement} was never written. Rename {} back to {replaced}, \
                         then run `ferro migrate new {name} --replace {replaced}` again",
                        show(aside)
                    )
                }
            }
            DirectoryError::InterruptedWrite { partial, migration } => write!(
                f,
                "{} is part of {migration}, left by an interrupted `ferro migrate new`; \
                 {migration} was never written. Delete {}, then run `ferro migrate new` \
                 again",
                show(partial),
                show(partial)
            ),
            DirectoryError::MissingRendering {
                migration,
                step,
                dialect,
            } => write!(
                f,
                "{}/{step} has no {} rendering, and the config targets {}; regenerate the \
                 migration with `ferro migrate new`, or write {step}.up.{}.sql and \
                 {step}.down.{}.sql",
                show(migration),
                dialect.suffix().unwrap_or("portable"),
                dialect.suffix().unwrap_or("portable"),
                dialect.suffix().unwrap_or("portable"),
                dialect.suffix().unwrap_or("portable"),
            ),
        }
    }
}

impl std::error::Error for DirectoryError {}

/// `a or b`, `a, b or c`.
fn or_list(items: &[String]) -> String {
    match items {
        [] => String::new(),
        [only] => only.clone(),
        [init @ .., last] => format!("{} or {last}", init.join(", ")),
    }
}

/// `ferro migrate new <name> --replace NNNN_<name>` for the migration
/// directory `dir`: the command that regenerates it on top of the newest
/// snapshot below it.
fn replace_line(dir: &Path) -> String {
    let full = dir
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| dir.display().to_string());
    let name = split_numbered(&full, 4).map_or_else(|| full.clone(), |(_, name)| name);
    format!("ferro migrate new {name} --replace {full}")
}

fn serialize_checksum<S: serde::Serializer>(
    checksum: &[u8; 48],
    serializer: S,
) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(&encode_checksum(checksum))
}

fn serialize_optional_checksum<S: serde::Serializer>(
    checksum: &Option<[u8; 48]>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    match checksum {
        Some(checksum) => serializer.serialize_str(&encode_checksum(checksum)),
        None => serializer.serialize_none(),
    }
}

pub(crate) fn serialize_snapshot<S: serde::Serializer>(
    snapshot: &Snapshot,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    use serde::Serialize;
    serde_json::json!({
        "checksum": encode_checksum(&snapshot.checksum),
        "parent_checksum": snapshot.parent_checksum.as_ref().map(encode_checksum),
        "ir": snapshot.ir,
    })
    .serialize(serializer)
}

/// One parsed step-file name.
struct StepFileName {
    ordinal: u8,
    name: String,
    kind: StepKind,
    dialect: StepDialect,
    down: bool,
}

const STEP_FILE_SHAPES: &str = "NN_<name>.<up|down>.<postgres|sqlite>.sql, \
     NN_<name>.<up|down>.sql, NN_<name>.py or ir.json";

fn is_name(name: &str) -> bool {
    !name.is_empty() && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// `NN_<name>` → `(NN, name)` for a `digits`-digit number.
fn split_numbered(stem: &str, digits: usize) -> Option<(u32, String)> {
    let (number, name) = stem.split_at_checked(digits)?;
    let name = name.strip_prefix('_')?;
    if !number.chars().all(|c| c.is_ascii_digit()) || !is_name(name) {
        return None;
    }
    Some((number.parse().ok()?, name.to_string()))
}

fn parse_step_file_name(file_name: &str) -> Option<StepFileName> {
    if let Some(stem) = file_name.strip_suffix(".py") {
        let (ordinal, name) = split_numbered(stem, 2)?;
        return Some(StepFileName {
            ordinal: u8::try_from(ordinal).ok()?,
            name,
            kind: StepKind::Data,
            dialect: StepDialect::Portable,
            down: false,
        });
    }
    let stem = file_name.strip_suffix(".sql")?;
    let parts: Vec<&str> = stem.split('.').collect();
    let (base, direction, dialect) = match parts.as_slice() {
        [base, direction] => (*base, *direction, StepDialect::Portable),
        [base, direction, suffix] => (*base, *direction, StepDialect::from_suffix(suffix)?),
        _ => return None,
    };
    let down = match direction {
        "up" => false,
        "down" => true,
        _ => return None,
    };
    let (ordinal, name) = split_numbered(base, 2)?;
    Some(StepFileName {
        ordinal: u8::try_from(ordinal).ok()?,
        name,
        kind: if dialect == StepDialect::Portable {
            StepKind::PortableSql
        } else {
            StepKind::Ddl
        },
        dialect,
        down,
    })
}

/// Which way a step file runs.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum StepDirection {
    /// `.up.` (and every data step: its `down()` lives in the same file).
    Up,
    /// `.down.`
    Down,
}

/// What a step file's name says it is: `01_expand.down.postgres.sql` is a
/// `ddl` step's `down` file for `postgres`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
pub struct StepFileRole {
    /// The step's kind.
    pub kind: StepKind,
    /// Which way the file runs.
    pub direction: StepDirection,
    /// The dialect the file serves.
    pub dialect: StepDialect,
}

/// The kind, direction and dialect a step file's name says, or `None` for a
/// name that is not a step file's (`ir.json` included): the one reading of a
/// step file's name, shared by the directory reader and `ferro migrate`'s
/// output.
pub fn step_file_role(file_name: &str) -> Option<StepFileRole> {
    parse_step_file_name(file_name).map(|parsed| StepFileRole {
        kind: parsed.kind,
        direction: if parsed.down {
            StepDirection::Down
        } else {
            StepDirection::Up
        },
        dialect: parsed.dialect,
    })
}

fn ignored(name: &str) -> bool {
    name.starts_with('.') || name.starts_with('_')
}

/// The entries of `dir`, sorted by name, without the ignored ones.
fn entries(root: &Path, dir: &Path) -> Result<Vec<(String, PathBuf)>, DirectoryError> {
    let unreadable = |path: &Path, err: std::io::Error| DirectoryError::Unreadable {
        path: relative(root, path),
        reason: err.to_string(),
    };
    let mut out = Vec::new();
    for entry in std::fs::read_dir(dir).map_err(|err| unreadable(dir, err))? {
        let entry = entry.map_err(|err| unreadable(dir, err))?;
        let name = entry.file_name().to_string_lossy().to_string();
        if !ignored(&name) {
            out.push((name, entry.path()));
        }
    }
    out.sort();
    Ok(out)
}

fn relative(root: &Path, path: &Path) -> PathBuf {
    path.strip_prefix(root)
        .map(Path::to_path_buf)
        .unwrap_or_else(|_| path.to_path_buf())
}

fn read_bytes(root: &Path, path: &Path) -> Result<Vec<u8>, DirectoryError> {
    std::fs::read(path).map_err(|err| DirectoryError::Unreadable {
        path: relative(root, path),
        reason: err.to_string(),
    })
}

fn read_headers(root: &Path, path: &Path, bytes: &[u8]) -> Result<Headers, DirectoryError> {
    Headers::parse(&String::from_utf8_lossy(bytes)).map_err(|line| {
        DirectoryError::UnparseableHeader {
            file: relative(root, path),
            line,
        }
    })
}

impl MigrationsDir {
    /// Read and verify the migrations directory at `path`. A directory that
    /// does not exist holds no migrations.
    ///
    /// # Errors
    /// The first [`DirectoryError`] met: an unexpected entry, a duplicate or
    /// missing migration or step number, a missing or unreadable snapshot, a
    /// broken snapshot chain, a step with both suffixed and unsuffixed files,
    /// an up without its down, or an unparseable header.
    pub fn read(path: &Path) -> Result<MigrationsDir, DirectoryError> {
        let root = path;
        if !root.exists() {
            return Ok(MigrationsDir {
                path: root.to_path_buf(),
                migrations: Vec::new(),
            });
        }
        interrupted_writes(root)?;
        assemble(root, unique_numbers(root, migration_dirs(root)?)?)
    }

    /// Read the migrations directory at `path` for `ferro migrate new
    /// --replace <target>`: the directory without the migration `target`
    /// names, read and verified as [`MigrationsDir::read`] reads it, and that
    /// migration, read whole. `target` is `NNNN_<name>`, or `NNNN` when one
    /// migration carries the number. The one duplicate admitted is at the
    /// replaced number (two branches each generated it); the replaced
    /// migration's own parent link is not checked, since its parent moving
    /// under it is why it is replaced. It is the run of length one: the run
    /// from `target` ([`MigrationsDir::read_run`]) with the refusal of a
    /// longer one.
    ///
    /// # Errors
    /// [`DirectoryError::AmbiguousNumber`] for a bare number two migrations
    /// share, [`DirectoryError::NoSuchMigration`] for a target the directory
    /// does not hold, [`DirectoryError::LaterMigration`] when a later
    /// migration chains from it, and what [`MigrationsDir::read`] raises for
    /// the rest of the directory and for the replaced migration's own files.
    pub fn read_replacing(
        path: &Path,
        target: &str,
    ) -> Result<(MigrationsDir, Migration), DirectoryError> {
        let (dir, mut run) = MigrationsDir::read_run(path, target)?;
        if let [replaced, later, ..] = run.as_slice() {
            return Err(DirectoryError::LaterMigration {
                replaced: replaced.dir_name(),
                later: later.dir_name(),
            });
        }
        let replaced = run.remove(0);
        Ok((dir, replaced))
    }

    /// Read the migrations directory at `path` for a squash: the directory
    /// below the migration `from` names, read and verified as
    /// [`MigrationsDir::read`] reads it, and the run, `from` through the head,
    /// in chain order, each migration read whole. `from` is `NNNN_<name>`, or
    /// `NNNN` when one migration carries the number. A run is always a tail
    /// of the directory (ADR-0023). The duplicate admitted at `from`'s number
    /// and the unchecked parent link of the run's first migration are
    /// [`MigrationsDir::read_replacing`]'s; every later migration of the run
    /// must chain from the one before it.
    ///
    /// # Errors
    /// [`DirectoryError::AmbiguousNumber`] for a bare number two migrations
    /// share, [`DirectoryError::NoSuchMigration`] for a target the directory
    /// does not hold, and what [`MigrationsDir::read`] raises for the
    /// directory below the run and for the run's own files, numbers and
    /// chain.
    pub fn read_run(
        path: &Path,
        from: &str,
    ) -> Result<(MigrationsDir, Vec<Migration>), DirectoryError> {
        let root = path;
        let target = from;
        let no_such = || DirectoryError::NoSuchMigration {
            target: target.to_string(),
        };
        if !root.exists() {
            return Err(no_such());
        }
        interrupted_writes(root)?;
        let mut dirs = migration_dirs(root)?;
        let full = |(number, name, _): &(u16, String, PathBuf)| format!("{number:04}_{name}");
        let matching: Vec<usize> = match target.parse::<u16>() {
            Ok(number) if target.len() == 4 && target.chars().all(|c| c.is_ascii_digit()) => dirs
                .iter()
                .enumerate()
                .filter(|(_, (n, _, _))| *n == number)
                .map(|(at, _)| at)
                .collect(),
            _ => dirs
                .iter()
                .enumerate()
                .filter(|(_, dir)| full(dir) == target)
                .map(|(at, _)| at)
                .collect(),
        };
        let at = match matching.as_slice() {
            [] => return Err(no_such()),
            [at] => *at,
            several => {
                return Err(DirectoryError::AmbiguousNumber {
                    number: dirs[several[0]].0,
                    names: several.iter().map(|at| full(&dirs[*at])).collect(),
                });
            }
        };
        let (number, name, dir) = dirs.remove(at);
        let first = read_migration(root, number, name, dir)?;
        // The run's later migrations, by number; the rest stays below.
        let (later, below): (Vec<_>, Vec<_>) = dirs.into_iter().partition(|(n, _, _)| *n > number);
        let mut run = vec![first];
        for (expected, (n, (name, dir))) in (number + 1..).zip(unique_numbers(root, later)?) {
            if n != expected {
                return Err(DirectoryError::MissingNumber {
                    number: expected,
                    next: relative(root, &dir),
                });
            }
            let migration = read_migration(root, n, name, dir)?;
            let parent = run.last().map(|p| p.snapshot.checksum);
            if migration.snapshot.parent_checksum != parent {
                return Err(DirectoryError::BrokenChain {
                    parent: run.last().map(|p| relative(root, &p.dir.join(SNAPSHOT_FILE))),
                    child: relative(root, &migration.dir.join(SNAPSHOT_FILE)),
                });
            }
            run.push(migration);
        }
        let dir = assemble(root, unique_numbers(root, below)?)?;
        Ok((dir, run))
    }

    /// The newest migration, when there is one.
    pub fn head(&self) -> Option<&Migration> {
        self.migrations.last()
    }
}

/// Refuse what an interrupted `ferro migrate new` left under a hidden name
/// (panel F6): a migration `--replace` set aside and never deleted
/// (`.NNNN_<name>.replaced`, `.NNNN_<name>.MMMM_<new>.replaced`), ahead of a
/// half-written one (`.NNNN_<name>.partial`), since the aside may hold the
/// only copy of hand-written steps. Other hidden names stay ignored.
fn interrupted_writes(root: &Path) -> Result<(), DirectoryError> {
    let unreadable = |err: std::io::Error| DirectoryError::Unreadable {
        path: PathBuf::from("."),
        reason: err.to_string(),
    };
    let migration_name = |name: &str| split_numbered(name, 4).map(|_| name.to_string());
    let mut asides = Vec::new();
    let mut partials = Vec::new();
    for entry in std::fs::read_dir(root).map_err(unreadable)? {
        let file_name = entry.map_err(unreadable)?.file_name();
        let file_name = file_name.to_string_lossy();
        let Some(hidden) = file_name.strip_prefix('.') else {
            continue;
        };
        if let Some(names) = hidden.strip_suffix(".replaced") {
            let (replaced, replacement) = names.split_once('.').unwrap_or((names, names));
            if let (Some(replaced), Some(replacement)) =
                (migration_name(replaced), migration_name(replacement))
            {
                let complete = root.join(&replacement).is_dir();
                asides.push(DirectoryError::InterruptedReplace {
                    aside: PathBuf::from(file_name.as_ref()),
                    replaced,
                    replacement,
                    complete,
                });
            }
        } else if let Some(migration) = hidden.strip_suffix(".partial").and_then(migration_name) {
            partials.push(DirectoryError::InterruptedWrite {
                partial: PathBuf::from(file_name.as_ref()),
                migration,
            });
        }
    }
    let first = |mut found: Vec<DirectoryError>| {
        found.sort_by_key(ToString::to_string);
        found.into_iter().next()
    };
    match first(asides).or_else(|| first(partials)) {
        Some(err) => Err(err),
        None => Ok(()),
    }
}

/// Every migration directory under `root` as `(number, name, path)`, in
/// name order; any other entry is refused.
fn migration_dirs(root: &Path) -> Result<Vec<(u16, String, PathBuf)>, DirectoryError> {
    let mut out = Vec::new();
    for (name, entry) in entries(root, root)? {
        if !entry.is_dir() {
            continue;
        }
        let Some((number, migration_name)) = split_numbered(&name, 4)
            .and_then(|(n, rest)| Some((u16::try_from(n).ok()?, rest)))
            .filter(|(n, _)| *n > 0)
        else {
            return Err(DirectoryError::UnexpectedEntry {
                path: PathBuf::from(&name),
                expected: "a migration directory named NNNN_<name>",
            });
        };
        out.push((number, migration_name, entry));
    }
    Ok(out)
}

/// `dirs` by number, refusing two that share one.
fn unique_numbers(
    root: &Path,
    dirs: Vec<(u16, String, PathBuf)>,
) -> Result<BTreeMap<u16, (String, PathBuf)>, DirectoryError> {
    let mut numbered: BTreeMap<u16, (String, PathBuf)> = BTreeMap::new();
    for (number, name, entry) in dirs {
        if let Some((_, first)) = numbered.get(&number) {
            return Err(DirectoryError::DuplicateNumber {
                number,
                first: relative(root, first),
                second: relative(root, &entry),
            });
        }
        numbered.insert(number, (name, entry));
    }
    Ok(numbered)
}

/// Read every migration of `numbered` and verify the numbers are dense from
/// `0001` and the snapshots chained.
fn assemble(
    root: &Path,
    numbered: BTreeMap<u16, (String, PathBuf)>,
) -> Result<MigrationsDir, DirectoryError> {
    let mut migrations: Vec<Migration> = Vec::new();
    for (expected, (number, (name, dir))) in (1u16..).zip(numbered) {
        if number != expected {
            return Err(DirectoryError::MissingNumber {
                number: expected,
                next: relative(root, &dir),
            });
        }
        let migration = read_migration(root, number, name, dir)?;
        let parent = migrations.last();
        if migration.snapshot.parent_checksum != parent.map(|p| p.snapshot.checksum) {
            return Err(DirectoryError::BrokenChain {
                parent: parent.map(|p| relative(root, &p.dir.join(SNAPSHOT_FILE))),
                child: relative(root, &migration.dir.join(SNAPSHOT_FILE)),
            });
        }
        migrations.push(migration);
    }
    Ok(MigrationsDir {
        path: root.to_path_buf(),
        migrations,
    })
}

impl MigrationsDir {
    /// Every DDL step that lacks a rendering for one of `dialects`, as
    /// [`DirectoryError::MissingRendering`]s in migration and step order.
    pub fn missing_renderings(&self, dialects: &[Dialect]) -> Vec<DirectoryError> {
        let mut missing = Vec::new();
        for migration in &self.migrations {
            for step in migration.steps.iter().filter(|s| s.kind == StepKind::Ddl) {
                for dialect in dialects {
                    let key = StepDialect::from(*dialect);
                    if !step.files.contains_key(&key) {
                        missing.push(DirectoryError::MissingRendering {
                            migration: relative(&self.path, &migration.dir),
                            step: format!("{:02}_{}", step.ordinal, step.name),
                            dialect: key,
                        });
                    }
                }
            }
        }
        missing
    }
}

fn read_migration(
    root: &Path,
    number: u16,
    name: String,
    dir: PathBuf,
) -> Result<Migration, DirectoryError> {
    let mut snapshot = None;
    // ordinal -> (name, first file seen, kind, files)
    let mut files: BTreeMap<u8, Vec<(StepFileName, PathBuf)>> = BTreeMap::new();
    for (file_name, path) in entries(root, &dir)? {
        if file_name == SNAPSHOT_FILE {
            let bytes = read_bytes(root, &path)?;
            snapshot = Some(Snapshot::load(&bytes).map_err(|error| {
                DirectoryError::UnreadableSnapshot {
                    path: relative(root, &path),
                    error,
                }
            })?);
            continue;
        }
        let parsed = parse_step_file_name(&file_name)
            .filter(|parsed| parsed.ordinal > 0 && path.is_file())
            .ok_or_else(|| DirectoryError::UnexpectedEntry {
                path: relative(root, &path),
                expected: STEP_FILE_SHAPES,
            })?;
        files
            .entry(parsed.ordinal)
            .or_default()
            .push((parsed, path));
    }
    let snapshot = snapshot.ok_or_else(|| DirectoryError::MissingSnapshot {
        migration: relative(root, &dir),
    })?;

    let mut steps = Vec::new();
    for (expected, (ordinal, step_files)) in (1u8..).zip(files) {
        if ordinal != expected {
            return Err(DirectoryError::MissingStep {
                migration: relative(root, &dir),
                ordinal: expected,
            });
        }
        steps.push(read_step(root, ordinal, step_files)?);
    }
    if steps.is_empty() {
        return Err(DirectoryError::MissingStep {
            migration: relative(root, &dir),
            ordinal: 1,
        });
    }
    Ok(Migration {
        number,
        name,
        dir,
        steps,
        snapshot,
    })
}

fn read_step(
    root: &Path,
    ordinal: u8,
    step_files: Vec<(StepFileName, PathBuf)>,
) -> Result<Step, DirectoryError> {
    let (first, first_path) = &step_files[0];
    let name = first.name.clone();
    // Two names, or a data step beside SQL, at one ordinal: two steps.
    let clash = step_files.iter().find(|(parsed, _)| {
        parsed.name != name || (parsed.kind == StepKind::Data) != (first.kind == StepKind::Data)
    });
    if let Some((_, other)) = clash {
        return Err(DirectoryError::DuplicateStep {
            first: relative(root, first_path),
            second: relative(root, other),
        });
    }
    // Name the up files when there are some: they are what a reader opens.
    let find = |kind: StepKind| {
        step_files
            .iter()
            .filter(|(parsed, _)| parsed.kind == kind)
            .min_by_key(|(parsed, _)| parsed.down)
    };
    let portable = find(StepKind::PortableSql);
    let suffixed = find(StepKind::Ddl);
    if let (Some((_, unsuffixed)), Some((_, suffixed))) = (portable, suffixed) {
        return Err(DirectoryError::BothSuffixedAndUnsuffixed {
            suffixed: relative(root, suffixed),
            unsuffixed: relative(root, unsuffixed),
        });
    }
    if first.kind == StepKind::Data {
        if let Some((_, second)) = step_files.get(1) {
            return Err(DirectoryError::DuplicateStep {
                first: relative(root, first_path),
                second: relative(root, second),
            });
        }
        let bytes = read_bytes(root, first_path)?;
        let mut files = BTreeMap::new();
        files.insert(
            StepDialect::Portable,
            StepFile {
                up: first_path.clone(),
                down: None,
                up_checksum: sha384(&bytes),
                headers: Headers::default(),
                down_checksum: None,
                down_headers: Headers::default(),
            },
        );
        return Ok(Step {
            ordinal,
            name,
            kind: StepKind::Data,
            files,
        });
    }

    let mut ups: BTreeMap<StepDialect, PathBuf> = BTreeMap::new();
    let mut downs: BTreeMap<StepDialect, PathBuf> = BTreeMap::new();
    for (parsed, path) in &step_files {
        let side = if parsed.down { &mut downs } else { &mut ups };
        side.insert(parsed.dialect, path.clone());
    }
    let pair_name = |dialect: StepDialect, down: bool| {
        let direction = if down { "down" } else { "up" };
        match dialect.suffix() {
            Some(suffix) => format!("{ordinal:02}_{name}.{direction}.{suffix}.sql"),
            None => format!("{ordinal:02}_{name}.{direction}.sql"),
        }
    };
    let mut files = BTreeMap::new();
    for (dialect, down) in &downs {
        if !ups.contains_key(dialect) {
            return Err(DirectoryError::MissingPair {
                present: relative(root, down),
                missing: relative(root, &down.with_file_name(pair_name(*dialect, false))),
            });
        }
    }
    for (dialect, up) in ups {
        let down = downs
            .get(&dialect)
            .ok_or_else(|| DirectoryError::MissingPair {
                present: relative(root, &up),
                missing: relative(root, &up.with_file_name(pair_name(dialect, true))),
            })?
            .clone();
        let up_bytes = read_bytes(root, &up)?;
        let headers = read_headers(root, &up, &up_bytes)?;
        let down_bytes = read_bytes(root, &down)?;
        let down_headers = read_headers(root, &down, &down_bytes)?;
        files.insert(
            dialect,
            StepFile {
                up_checksum: sha384(&up_bytes),
                up,
                down: Some(down),
                headers,
                down_checksum: Some(sha384(&down_bytes)),
                down_headers,
            },
        );
    }
    Ok(Step {
        ordinal,
        name,
        kind: first.kind,
        files,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
    use std::sync::atomic::{AtomicUsize, Ordering};

    static COUNTER: AtomicUsize = AtomicUsize::new(0);

    /// A fresh, empty directory under the system temp dir.
    fn scratch(test: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "ferro-migrate-dir-{}-{}-{test}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("scratch dir");
        dir
    }

    #[test]
    fn a_step_files_name_says_its_kind_direction_and_dialect() {
        let role = |kind, direction, dialect| StepFileRole {
            kind,
            direction,
            dialect,
        };
        for (name, expected) in [
            (
                "01_expand.up.postgres.sql",
                role(StepKind::Ddl, StepDirection::Up, StepDialect::Postgres),
            ),
            (
                "01_expand.down.sqlite.sql",
                role(StepKind::Ddl, StepDirection::Down, StepDialect::Sqlite),
            ),
            (
                "02_fix.up.sql",
                role(
                    StepKind::PortableSql,
                    StepDirection::Up,
                    StepDialect::Portable,
                ),
            ),
            (
                "02_fix.down.sql",
                role(
                    StepKind::PortableSql,
                    StepDirection::Down,
                    StepDialect::Portable,
                ),
            ),
            (
                "03_backfill_author.py",
                role(StepKind::Data, StepDirection::Up, StepDialect::Portable),
            ),
        ] {
            assert_eq!(step_file_role(name), Some(expected), "{name}");
        }
        for name in [
            SNAPSHOT_FILE,
            "01_expand.sideways.sql",
            "01_expand.up.mysql.sql",
            "expand.up.sql",
            "01_expand.up.postgres.sql.bak",
        ] {
            assert_eq!(step_file_role(name), None, "{name}");
        }
        assert_eq!(
            serde_json::to_value(step_file_role("01_e.down.postgres.sql")).expect("serialises"),
            serde_json::json!({"kind": "ddl", "direction": "down", "dialect": "postgres"})
        );
    }

    fn empty_ir() -> IrEnvelope<SchemaIrPayload> {
        IrEnvelope {
            ir_kind: "schema".into(),
            ir_version: 1,
            payload: SchemaIrPayload {
                dialect_agnostic: true,
                models: Vec::new(),
            },
        }
    }

    fn write(path: &Path, text: &str) {
        std::fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
        std::fs::write(path, text).expect("write");
    }

    /// Write a migration with one DDL step on both dialects, chained to
    /// `parent` (the previous ir.json bytes); returns its ir.json bytes.
    fn migration(root: &Path, dir_name: &str, parent: Option<&[u8]>) -> Vec<u8> {
        let dir = root.join(dir_name);
        for dialect in ["postgres", "sqlite"] {
            write(
                &dir.join(format!("01_schema.up.{dialect}.sql")),
                "CREATE TABLE \"t\" (\"id\" integer);\n",
            );
            write(
                &dir.join(format!("01_schema.down.{dialect}.sql")),
                "-- ferro: data-dependent\n\nDROP TABLE \"t\";\n",
            );
        }
        let ir = Snapshot::store(&empty_ir(), parent.map(sha384)).expect("store");
        std::fs::write(dir.join(SNAPSHOT_FILE), &ir).expect("ir.json");
        ir
    }

    #[test]
    fn a_missing_directory_holds_no_migrations() {
        let root = scratch("missing").join("migrations");
        let read = MigrationsDir::read(&root).expect("read");
        assert!(read.migrations.is_empty());
    }

    #[test]
    fn reads_numbers_steps_dialects_headers_and_the_chain() {
        let root = scratch("happy");
        write(&root.join(".gitattributes"), "* -text\n");
        let first = migration(&root, "0001_create_author", None);
        migration(&root, "0002_more", Some(&first));
        write(
            &root.join("0002_more/02_fix_rows.up.sql"),
            "-- write this step\n",
        );
        write(
            &root.join("0002_more/02_fix_rows.down.sql"),
            "-- write this step\n",
        );
        write(&root.join("0002_more/03_backfill_author.py"), "# data\n");
        std::fs::create_dir_all(root.join("0002_more/__pycache__")).expect("pycache");

        let read = MigrationsDir::read(&root).expect("read");
        assert_eq!(read.migrations.len(), 2);
        let head = read.head().expect("head");
        assert_eq!((head.number, head.name.as_str()), (2, "more"));
        assert_eq!(head.dir_name(), "0002_more");
        let kinds: Vec<(u8, &str, StepKind)> = head
            .steps
            .iter()
            .map(|s| (s.ordinal, s.name.as_str(), s.kind))
            .collect();
        assert_eq!(
            kinds,
            [
                (1, "schema", StepKind::Ddl),
                (2, "fix_rows", StepKind::PortableSql),
                (3, "backfill_author", StepKind::Data)
            ]
        );
        let schema = &head.steps[0];
        assert_eq!(
            schema.files.keys().copied().collect::<Vec<_>>(),
            [StepDialect::Postgres, StepDialect::Sqlite]
        );
        let pg = &schema.files[&StepDialect::Postgres];
        assert_eq!(
            pg.up_checksum,
            sha384(b"CREATE TABLE \"t\" (\"id\" integer);\n")
        );
        assert!(
            pg.down
                .as_ref()
                .unwrap()
                .ends_with("01_schema.down.postgres.sql")
        );
        assert!(pg.down_headers.data_dependent && !pg.headers.data_dependent);
        assert_eq!(
            pg.down_checksum,
            Some(sha384(b"-- ferro: data-dependent\n\nDROP TABLE \"t\";\n"))
        );
        let data = &head.steps[2].files[&StepDialect::Portable];
        assert_eq!(
            (data.down_checksum, &data.down_headers),
            (None, &Headers::default())
        );
        assert_eq!(head.snapshot.parent_checksum, Some(sha384(&first)));
        assert!(
            read.missing_renderings(&[Dialect::Postgres, Dialect::Sqlite])
                .is_empty()
        );
    }

    #[test]
    fn headers_parse_and_render_round_trip() {
        let headers = Headers {
            no_transaction: true,
            foreign_keys_off: true,
            destructive: true,
            data_dependent: true,
            not_applicable: true,
            expand: true,
            nothing_to_reverse: Some("the up drops nothing".into()),
            irreversible: Some("rows are gone".into()),
        };
        let text = format!("{}\nCREATE TABLE x;\n-- ferro: bogus\n", headers.render());
        assert_eq!(Headers::parse(&text), Ok(headers));
        assert_eq!(
            Headers::parse("-- ferro: destructive\n-- ferro: sometimes\n"),
            Err("-- ferro: sometimes".to_string())
        );
        assert_eq!(
            Headers::parse("-- ferro: irreversible\n"),
            Err("-- ferro: irreversible".to_string()),
            "a reason is required"
        );
        // ADR-0033 spells the reason after the token, as a person writes it.
        assert_eq!(
            Headers::parse(
                "-- ferro: irreversible dropped rows cannot come back\n\
                 -- ferro: nothing-to-reverse the label stays\n"
            ),
            Ok(Headers {
                irreversible: Some("dropped rows cannot come back".into()),
                nothing_to_reverse: Some("the label stays".into()),
                ..Headers::default()
            })
        );
        assert_eq!(
            Headers {
                irreversible: Some("rows are gone".into()),
                ..Headers::default()
            }
            .render(),
            "-- ferro: irreversible rows are gone\n"
        );
        assert_eq!(
            Headers::parse("-- ferro: destructive now\n"),
            Err("-- ferro: destructive now".to_string()),
            "a flag takes no reason"
        );
    }

    #[test]
    fn a_hand_step_declares_itself_expand_and_the_flag_takes_no_text() {
        // ADR-0054: a hand-written SQL step is contract unless its author
        // writes `-- ferro: expand` beside the SQL.
        assert_eq!(
            Headers::parse(
                "-- ferro: expand
CREATE INDEX i ON t (c);
"
            ),
            Ok(Headers {
                expand: true,
                ..Headers::default()
            })
        );
        assert!(
            !Headers::parse(
                "UPDATE t SET c = 1;
"
            )
            .unwrap()
            .expand
        );
        assert_eq!(
            Headers::parse(
                "-- ferro: expand now
"
            ),
            Err("-- ferro: expand now".to_string()),
            "a flag takes no text after it"
        );
    }

    #[test]
    fn a_leftover_replace_aside_or_partial_write_is_refused_by_every_read() {
        // Panel F6: a kill mid `--replace` leaves the old migration under a
        // hidden name the reader would otherwise skip.
        let root = scratch("aside");
        let first = migration(&root, "0001_a", None);
        migration(&root, ".0002_b.replaced", Some(&first));
        for err in [
            MigrationsDir::read(&root).expect_err("aside"),
            MigrationsDir::read_replacing(&root, "0001_a").expect_err("aside"),
        ] {
            assert_eq!(err.kind(), "interrupted_replace");
            assert_eq!(
                err.to_string(),
                ".0002_b.replaced is 0002_b, set aside by an interrupted `ferro migrate \
                 new --replace 0002_b`; 0002_b was never written. Rename .0002_b.replaced \
                 back to 0002_b, then run `ferro migrate new b --replace 0002_b` again"
            );
        }
        // Renumbered and complete: the aside names its replacement.
        std::fs::rename(
            root.join(".0002_b.replaced"),
            root.join(".0002_b.0002_c.replaced"),
        )
        .expect("rename");
        migration(&root, "0002_c", Some(&first));
        let err = MigrationsDir::read(&root).expect_err("aside");
        assert_eq!(
            err.to_string(),
            ".0002_b.0002_c.replaced is 0002_b, set aside by an interrupted `ferro \
             migrate new --replace 0002_b`; its replacement 0002_c is complete. Delete \
             .0002_b.0002_c.replaced"
        );
        // The aside comes first; the partial once it is gone.
        migration(&root, ".0003_d.partial", None);
        assert_eq!(
            MigrationsDir::read(&root).expect_err("aside").kind(),
            "interrupted_replace"
        );
        std::fs::remove_dir_all(root.join(".0002_b.0002_c.replaced")).expect("rm");
        let err = MigrationsDir::read(&root).expect_err("partial");
        assert_eq!(err.kind(), "interrupted_write");
        assert_eq!(
            err.to_string(),
            ".0003_d.partial is part of 0003_d, left by an interrupted `ferro migrate \
             new`; 0003_d was never written. Delete .0003_d.partial, then run `ferro \
             migrate new` again"
        );
        std::fs::remove_dir_all(root.join(".0003_d.partial")).expect("rm");
        assert_eq!(
            MigrationsDir::read(&root).expect("clean").migrations.len(),
            2
        );
    }

    #[test]
    fn a_duplicate_number_names_both_directories() {
        let root = scratch("dup");
        let first = migration(&root, "0001_a", None);
        migration(&root, "0002_a", Some(&first));
        migration(&root, "0002_b", Some(&first));
        let err = MigrationsDir::read(&root).expect_err("duplicate");
        assert_eq!(err.kind(), "duplicate_number");
        let message = err.to_string();
        assert!(
            message.contains("0002_a") && message.contains("0002_b"),
            "{message}"
        );
    }

    #[test]
    fn a_duplicate_number_prints_a_replace_line_for_each_directory() {
        let root = scratch("dup-replace");
        let first = migration(&root, "0001_a", None);
        migration(&root, "0002_add_x", Some(&first));
        migration(&root, "0002_add_y", Some(&first));
        let message = MigrationsDir::read(&root)
            .expect_err("duplicate")
            .to_string();
        assert!(
            message.contains("0002_add_x and 0002_add_y")
                && message.contains("\n  ferro migrate new add_x --replace 0002_add_x")
                && message.contains("\n  ferro migrate new add_y --replace 0002_add_y"),
            "{message}"
        );
    }

    #[test]
    fn reading_for_a_replace_admits_the_duplicate_at_the_replaced_number_only() {
        let root = scratch("replacing");
        let first = migration(&root, "0001_a", None);
        let x = migration(&root, "0002_add_x", Some(&first));
        migration(&root, "0002_add_y", Some(&first));

        let (dir, replaced) = MigrationsDir::read_replacing(&root, "0002_add_x").expect("read");
        assert_eq!(replaced.dir_name(), "0002_add_x");
        assert_eq!(replaced.snapshot.checksum, sha384(&x));
        let names: Vec<String> = dir.migrations.iter().map(Migration::dir_name).collect();
        assert_eq!(names, ["0001_a", "0002_add_y"]);

        // A bare number two directories share names both.
        let err = MigrationsDir::read_replacing(&root, "0002").expect_err("ambiguous");
        assert_eq!(err.kind(), "ambiguous_number");
        let message = err.to_string();
        assert!(
            message.contains("--replace 0002_add_x") && message.contains("--replace 0002_add_y"),
            "{message}"
        );

        // A name the directory does not hold.
        let err = MigrationsDir::read_replacing(&root, "0009_nothing").expect_err("unknown");
        assert_eq!(err.kind(), "no_such_migration");
        assert!(err.to_string().contains("0009_nothing"), "{err}");

        // A second duplicate elsewhere is still refused.
        migration(&root, "0001_b", None);
        let err = MigrationsDir::read_replacing(&root, "0002_add_x").expect_err("dup");
        assert_eq!(err.kind(), "duplicate_number");
    }

    #[test]
    fn a_bare_number_one_directory_carries_names_it() {
        let root = scratch("replacing-bare");
        let first = migration(&root, "0001_a", None);
        migration(&root, "0002_add_x", Some(&first));
        let (dir, replaced) = MigrationsDir::read_replacing(&root, "0002").expect("read");
        assert_eq!(replaced.dir_name(), "0002_add_x");
        assert_eq!(dir.migrations.len(), 1);

        // The replaced migration's own parent link is not checked: its
        // parent moved under it (a rebase), which is why it is replaced.
        let root = scratch("replacing-stale");
        migration(&root, "0001_a", None);
        migration(&root, "0002_add_x", Some(b"a stale parent"));
        let (_, replaced) = MigrationsDir::read_replacing(&root, "0002_add_x").expect("read");
        assert_eq!(replaced.name, "add_x");
    }

    #[test]
    fn a_migration_a_later_one_chains_from_is_not_replaced() {
        let root = scratch("replacing-later");
        let first = migration(&root, "0001_a", None);
        let x = migration(&root, "0002_add_x", Some(&first));
        migration(&root, "0003_later", Some(&x));
        let err = MigrationsDir::read_replacing(&root, "0002_add_x").expect_err("later");
        assert_eq!(err.kind(), "later_migration");
        let message = err.to_string();
        assert!(
            message.contains("0003_later")
                && message.contains("replace the later one first, or delete it"),
            "{message}"
        );
    }

    #[test]
    fn a_run_reads_from_the_named_migration_through_the_head_in_chain_order() {
        let root = scratch("run");
        let a = migration(&root, "0001_a", None);
        let b = migration(&root, "0002_b", Some(&a));
        let c = migration(&root, "0003_c", Some(&b));
        migration(&root, "0004_d", Some(&c));

        let (dir, run) = MigrationsDir::read_run(&root, "0002_b").expect("run");
        let names = |ms: &[Migration]| ms.iter().map(Migration::dir_name).collect::<Vec<_>>();
        assert_eq!(names(&dir.migrations), ["0001_a"]);
        assert_eq!(names(&run), ["0002_b", "0003_c", "0004_d"]);

        // A run of one is the head; a bare number names it too.
        let (dir, run) = MigrationsDir::read_run(&root, "0004").expect("run of one");
        assert_eq!(dir.migrations.len(), 3);
        assert_eq!(names(&run), ["0004_d"]);

        // The run from the first migration leaves the directory empty.
        let (dir, run) = MigrationsDir::read_run(&root, "0001_a").expect("whole");
        assert!(dir.migrations.is_empty());
        assert_eq!(run.len(), 4);

        // Replacing still refuses a longer run, naming the next migration.
        let err = MigrationsDir::read_replacing(&root, "0002_b").expect_err("later");
        assert_eq!(err.kind(), "later_migration");
        assert!(err.to_string().contains("0003_c chains from 0002_b"), "{err}");
        let (_, replaced) = MigrationsDir::read_replacing(&root, "0004_d").expect("head");
        assert_eq!(replaced.dir_name(), "0004_d");
    }

    #[test]
    fn a_run_refuses_a_shared_bare_number_and_an_absent_target() {
        let root = scratch("run-refusals");
        let a = migration(&root, "0001_a", None);
        migration(&root, "0002_x", Some(&a));
        migration(&root, "0002_y", Some(&a));
        let err = MigrationsDir::read_run(&root, "0002").expect_err("ambiguous");
        assert_eq!(err.kind(), "ambiguous_number");
        let message = err.to_string();
        assert!(
            message.contains("0002_x") && message.contains("0002_y"),
            "{message}"
        );
        let err = MigrationsDir::read_run(&root, "0009").expect_err("absent");
        assert_eq!(err.kind(), "no_such_migration");
        assert!(err.to_string().contains("0009"), "{err}");
    }

    #[test]
    fn a_malformed_file_inside_a_run_is_refused_naming_it() {
        let root = scratch("run-malformed");
        let a = migration(&root, "0001_a", None);
        let b = migration(&root, "0002_b", Some(&a));
        migration(&root, "0003_c", Some(&b));
        write(
            &root.join("0003_c/01_schema.up.postgres.sql"),
            "-- ferro: not a header\nSELECT 1;\n",
        );
        let err = MigrationsDir::read_run(&root, "0002_b").expect_err("malformed");
        assert_eq!(err.kind(), "unparseable_header");
        assert!(
            err.to_string().contains("0003_c/01_schema.up.postgres.sql"),
            "{err}"
        );
    }

    #[test]
    fn a_gap_names_the_missing_number() {
        let root = scratch("gap");
        let first = migration(&root, "0001_a", None);
        migration(&root, "0003_c", Some(&first));
        let err = MigrationsDir::read(&root).expect_err("gap");
        assert_eq!(err.kind(), "missing_number");
        let message = err.to_string();
        assert!(
            message.contains("0002") && message.contains("0003_c"),
            "{message}"
        );

        let root = scratch("no-first");
        migration(&root, "0002_b", None);
        let err = MigrationsDir::read(&root).expect_err("no 0001");
        assert!(err.to_string().contains("0001"), "{err}");
    }

    #[test]
    fn an_edited_snapshot_breaks_the_chain_naming_both_files() {
        let root = scratch("chain");
        let first = migration(&root, "0001_a", None);
        migration(&root, "0002_b", Some(&first));
        // Edit 0001's snapshot: 0002's link no longer matches.
        let mut edited = first.clone();
        edited.extend_from_slice(b" ");
        std::fs::write(root.join("0001_a/ir.json"), &edited).expect("edit");
        let err = MigrationsDir::read(&root).expect_err("chain");
        assert_eq!(err.kind(), "broken_chain");
        let message = err.to_string();
        assert!(
            message.contains("0001_a/ir.json") && message.contains("0002_b/ir.json"),
            "{message}"
        );

        let root = scratch("chain-root");
        migration(&root, "0001_a", Some(b"not the parent"));
        let err = MigrationsDir::read(&root).expect_err("0001 with a parent");
        assert_eq!(err.kind(), "broken_chain");
    }

    #[test]
    fn a_step_with_both_suffixed_and_unsuffixed_files_is_refused_naming_both() {
        let root = scratch("both");
        migration(&root, "0001_a", None);
        write(&root.join("0001_a/01_schema.up.sql"), "SELECT 1;\n");
        write(&root.join("0001_a/01_schema.down.sql"), "SELECT 1;\n");
        let err = MigrationsDir::read(&root).expect_err("both");
        assert_eq!(err.kind(), "suffixed_and_unsuffixed");
        let message = err.to_string();
        assert!(
            message.contains("0001_a/01_schema.up.sql")
                && message.contains("0001_a/01_schema.up.postgres.sql"),
            "{message}"
        );
    }

    #[test]
    fn step_refusals_name_the_file() {
        let root = scratch("missing-step");
        migration(&root, "0001_a", None);
        write(&root.join("0001_a/03_x.up.sql"), "SELECT 1;\n");
        write(&root.join("0001_a/03_x.down.sql"), "SELECT 1;\n");
        let err = MigrationsDir::read(&root).expect_err("gap");
        assert_eq!(err.kind(), "missing_step");
        assert!(err.to_string().contains("step 02"), "{err}");

        let root = scratch("pair");
        migration(&root, "0001_a", None);
        std::fs::remove_file(root.join("0001_a/01_schema.down.sqlite.sql")).expect("rm");
        let err = MigrationsDir::read(&root).expect_err("pair");
        assert_eq!(err.kind(), "missing_pair");
        assert!(
            err.to_string().contains("01_schema.down.sqlite.sql"),
            "{err}"
        );

        let root = scratch("header");
        migration(&root, "0001_a", None);
        write(
            &root.join("0001_a/01_schema.up.postgres.sql"),
            "-- ferro: destrucive\nDROP TABLE x;\n",
        );
        let err = MigrationsDir::read(&root).expect_err("header");
        assert_eq!(err.kind(), "unparseable_header");
        assert!(err.to_string().contains("destrucive"), "{err}");

        let root = scratch("stray");
        migration(&root, "0001_a", None);
        write(&root.join("0001_a/notes.txt"), "hi\n");
        let err = MigrationsDir::read(&root).expect_err("stray");
        assert_eq!(err.kind(), "unexpected_entry");
        assert!(err.to_string().contains("notes.txt"), "{err}");

        let root = scratch("no-ir");
        migration(&root, "0001_a", None);
        std::fs::remove_file(root.join("0001_a/ir.json")).expect("rm");
        let err = MigrationsDir::read(&root).expect_err("no ir");
        assert_eq!(err.kind(), "missing_snapshot");
    }

    #[test]
    fn a_dialect_the_config_targets_but_a_step_lacks_is_a_missing_rendering() {
        let root = scratch("rendering");
        migration(&root, "0001_a", None);
        for direction in ["up", "down"] {
            std::fs::remove_file(root.join(format!("0001_a/01_schema.{direction}.sqlite.sql")))
                .expect("rm");
        }
        let read = MigrationsDir::read(&root).expect("read");
        assert!(read.missing_renderings(&[Dialect::Postgres]).is_empty());
        let missing = read.missing_renderings(&[Dialect::Postgres, Dialect::Sqlite]);
        assert_eq!(missing.len(), 1);
        assert_eq!(missing[0].kind(), "missing_rendering");
        let message = missing[0].to_string();
        assert!(
            message.contains("0001_a/01_schema") && message.contains("sqlite"),
            "{message}"
        );
    }
}
