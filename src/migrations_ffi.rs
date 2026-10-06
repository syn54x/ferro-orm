//! The in-house migration system's offline half over FFI: generating a
//! migration from two snapshots, reading a migrations directory, and the
//! offline check. JSON in, JSON out; every failure is a `ValueError` whose
//! message names the fix (AGENTS.md § I-3: nothing here panics).

use ferro_migrate::directory::MigrationsDir;
use ferro_migrate::snapshot::{Snapshot, encode_checksum};
use ferro_migrate::{Dialect, check_migrations, generate};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::path::Path;

/// Register every function of this module on the `_core` module.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(_generate_migration, m)?)?;
    m.add_function(wrap_pyfunction!(_check_migrations, m)?)?;
    m.add_function(wrap_pyfunction!(_read_migrations_dir, m)?)?;
    m.add_function(wrap_pyfunction!(_load_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(_store_snapshot, m)?)?;
    Ok(())
}

fn parse_dialects(dialects: &[String]) -> PyResult<Vec<Dialect>> {
    dialects
        .iter()
        .map(|dialect| match dialect.as_str() {
            "postgres" => Ok(Dialect::Postgres),
            "sqlite" => Ok(Dialect::Sqlite),
            other => Err(PyValueError::new_err(format!(
                "Unknown dialect {other:?}; expected 'postgres' or 'sqlite'"
            ))),
        })
        .collect()
}

fn parse_target(json: &str) -> PyResult<IrEnvelope<SchemaIrPayload>> {
    let envelope: IrEnvelope<SchemaIrPayload> = serde_json::from_str(json)
        .map_err(|e| PyValueError::new_err(format!("invalid target_ir_json: {e}")))?;
    if envelope.ir_kind != "schema" {
        return Err(PyValueError::new_err(format!(
            "target_ir_json: expected ir_kind 'schema', got '{}'",
            envelope.ir_kind
        )));
    }
    Ok(envelope)
}

fn load_snapshot(bytes: &[u8], what: &str) -> PyResult<Snapshot> {
    Snapshot::load(bytes).map_err(|err| PyValueError::new_err(format!("{what} {err}")))
}

fn to_json<T: serde::Serialize>(value: &T) -> PyResult<String> {
    serde_json::to_string(value)
        .map_err(|e| PyRuntimeError::new_err(format!("could not serialize the result: {e}")))
}

fn snapshot_json(snapshot: &Snapshot) -> serde_json::Value {
    serde_json::json!({
        "checksum": encode_checksum(&snapshot.checksum),
        "parent_checksum": snapshot.parent_checksum.as_ref().map(encode_checksum),
        "ir": snapshot.ir,
    })
}

/// Generate the migration that turns the head snapshot into the declared
/// modelset.
///
/// `parent_ir_json` is the head migration's `ir.json` text exactly as stored
/// (its checksum is the new snapshot's parent link), or `None` before the
/// first migration. Returns the JSON of the `GeneratedMigration`, or `None`
/// when nothing renders DDL (no schema change).
///
/// # Errors
/// `ValueError` naming the refusal: a change this generator does not generate
/// yet (`not generated yet: <op> on <table> (ticket #N)`), an unreadable
/// snapshot, an unknown dialect, or an op that cannot render.
#[pyfunction]
#[pyo3(name = "_generate_migration")]
#[pyo3(signature = (parent_ir_json, target_ir_json, dialects))]
pub fn _generate_migration(
    parent_ir_json: Option<String>,
    target_ir_json: String,
    dialects: Vec<String>,
) -> PyResult<Option<String>> {
    let dialects = parse_dialects(&dialects)?;
    let target = parse_target(&target_ir_json)?;
    let parent = parent_ir_json
        .map(|json| load_snapshot(json.as_bytes(), "the head snapshot"))
        .transpose()?;
    let generated = generate(parent.as_ref(), &target, &dialects)
        .map_err(|err| PyValueError::new_err(err.to_string()))?;
    generated.as_ref().map(to_json).transpose()
}

/// Check a migrations directory against the declared modelset, reading files
/// only. Returns JSON `{"ok": bool, "head": str | null, "problems":
/// [{"kind": str, "message": str}]}`.
///
/// # Errors
/// `ValueError` when the target IR or a dialect is malformed.
#[pyfunction]
#[pyo3(name = "_check_migrations")]
pub fn _check_migrations(
    directory: String,
    target_ir_json: String,
    dialects: Vec<String>,
) -> PyResult<String> {
    let dialects = parse_dialects(&dialects)?;
    let target = parse_target(&target_ir_json)?;
    to_json(&check_migrations(Path::new(&directory), &target, &dialects))
}

/// Read and verify a migrations directory. Returns the JSON of the
/// `MigrationsDir`: `{"path", "migrations": [{"number", "name", "dir",
/// "steps": [{"ordinal", "name", "kind", "files": {<dialect>: {"up", "down",
/// "up_checksum", "headers"}}}], "snapshot": {"checksum", "parent_checksum",
/// "ir"}}]}`.
///
/// # Errors
/// `ValueError` naming the problem and its fix: a duplicate or missing
/// number, a broken snapshot chain, a step with both suffixed and unsuffixed
/// files, an unparseable header, an unexpected entry.
#[pyfunction]
#[pyo3(name = "_read_migrations_dir")]
pub fn _read_migrations_dir(directory: String) -> PyResult<String> {
    let dir = MigrationsDir::read(Path::new(&directory))
        .map_err(|err| PyValueError::new_err(err.to_string()))?;
    to_json(&dir)
}

/// Load one `ir.json` (any shipped `ir_version`). Returns JSON
/// `{"checksum", "parent_checksum", "ir"}`.
///
/// # Errors
/// `ValueError` when the text is not a loadable snapshot.
#[pyfunction]
#[pyo3(name = "_load_snapshot")]
pub fn _load_snapshot(ir_json: String) -> PyResult<String> {
    let snapshot = load_snapshot(ir_json.as_bytes(), "the snapshot")?;
    to_json(&snapshot_json(&snapshot))
}

/// The canonical `ir.json` text of a migration that changes no schema: a
/// full copy of the parent's modelset, linked to the parent by its checksum
/// (ADR-0037). `parent_ir_json` is the parent's `ir.json` text as stored.
///
/// # Errors
/// `ValueError` when the parent is not a loadable snapshot.
#[pyfunction]
#[pyo3(name = "_store_snapshot")]
pub fn _store_snapshot(parent_ir_json: String) -> PyResult<String> {
    let parent = load_snapshot(parent_ir_json.as_bytes(), "the head snapshot")?;
    String::from_utf8(Snapshot::store(&parent.ir, Some(parent.checksum)))
        .map_err(|e| PyRuntimeError::new_err(format!("the stored snapshot is not UTF-8: {e}")))
}
