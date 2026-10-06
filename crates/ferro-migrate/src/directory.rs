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
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
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
    /// `-- ferro: nothing-to-reverse: <reason>`
    pub nothing_to_reverse: Option<String>,
    /// `-- ferro: irreversible: <reason>`
    pub irreversible: Option<String>,
}

/// Every header line opens with this.
pub const HEADER_PREFIX: &str = "-- ferro:";

impl Headers {
    /// Read the headers from a step file's text: every leading line that
    /// opens with `-- ferro:`. Reading stops at the first other line.
    ///
    /// # Errors
    /// The offending line, when a header line names no known header.
    pub fn parse(text: &str) -> Result<Headers, String> {
        let mut headers = Headers::default();
        for line in text.lines() {
            let Some(rest) = line.strip_prefix(HEADER_PREFIX) else {
                break;
            };
            let rest = rest.trim();
            let (token, reason) = match rest.split_once(':') {
                Some((token, reason)) => (token.trim(), Some(reason.trim().to_string())),
                None => (rest, None),
            };
            match (token, reason) {
                ("no-transaction", None) => headers.no_transaction = true,
                ("foreign-keys-off", None) => headers.foreign_keys_off = true,
                ("destructive", None) => headers.destructive = true,
                ("data-dependent", None) => headers.data_dependent = true,
                ("not-applicable", None) => headers.not_applicable = true,
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
        ];
        for (set, token) in flags {
            if set {
                lines.push(format!("{HEADER_PREFIX} {token}\n"));
            }
        }
        if let Some(reason) = &self.nothing_to_reverse {
            lines.push(format!("{HEADER_PREFIX} nothing-to-reverse: {reason}\n"));
        }
        if let Some(reason) = &self.irreversible {
            lines.push(format!("{HEADER_PREFIX} irreversible: {reason}\n"));
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
            } => write!(
                f,
                "two migrations are numbered {number:04}: {} and {}. Keep the one that is \
                 applied anywhere, delete the other and run `ferro migrate new` again to \
                 regenerate it on top",
                show(first),
                show(second)
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
                 foreign-keys-off, destructive, data-dependent, not-applicable, \
                 nothing-to-reverse: <reason> and irreversible: <reason>",
                show(file)
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

fn serialize_checksum<S: serde::Serializer>(
    checksum: &[u8; 48],
    serializer: S,
) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(&encode_checksum(checksum))
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
        let mut numbered: BTreeMap<u16, (String, PathBuf)> = BTreeMap::new();
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
            if let Some((_, first)) = numbered.get(&number) {
                return Err(DirectoryError::DuplicateNumber {
                    number,
                    first: relative(root, first),
                    second: PathBuf::from(&name),
                });
            }
            numbered.insert(number, (migration_name, entry));
        }

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

    /// The newest migration, when there is one.
    pub fn head(&self) -> Option<&Migration> {
        self.migrations.last()
    }

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
        read_headers(root, &down, &read_bytes(root, &down)?)?;
        files.insert(
            dialect,
            StepFile {
                up_checksum: sha384(&up_bytes),
                up,
                down: Some(down),
                headers,
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
                "-- ferro: destructive\n\nDROP TABLE \"t\";\n",
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
            Headers::parse("-- ferro: irreversible:\n"),
            Err("-- ferro: irreversible:".to_string()),
            "a reason is required"
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
