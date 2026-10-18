//! The schema snapshot (`ir.json`): the declared SchemaIR modelset a
//! migration was generated from, stored as canonical JSON with the SHA-384 of
//! its parent's `ir.json` (ADR-0023).

use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use std::collections::BTreeSet;

/// One loaded `ir.json`.
#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    /// The declared modelset.
    pub ir: IrEnvelope<SchemaIrPayload>,
    /// SHA-384 of the file's bytes as stored.
    pub checksum: [u8; 48],
    /// SHA-384 of the parent migration's `ir.json`; `None` for `0001`.
    pub parent_checksum: Option<[u8; 48]>,
    /// A squash's witness: one entry per migration it replaced, in chain
    /// order (ADR-0056). Empty for every snapshot that is not a squash.
    pub replaces: Vec<WitnessEntry>,
}

/// One migration a squash replaced, as its witness records it: what a
/// database that ran the originals must hold to adopt the squash (ADR-0056).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WitnessEntry {
    /// The replaced migration's directory name, `NNNN_<name>`.
    pub migration: String,
    /// SHA-384 of the replaced migration's `ir.json`, as stored.
    pub snapshot_checksum: [u8; 48],
    /// The ordinals of its steps, ascending.
    pub steps: Vec<u8>,
    /// When the replaced migration was itself a squash, its own witness.
    pub replaces: Vec<WitnessEntry>,
}

/// The witness's wire shape, in `ir.json` and over FFI alike: every entry
/// carries all four keys, `replaces` an empty list when it nests nothing.
impl serde::Serialize for WitnessEntry {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        use serde::ser::SerializeMap;
        let mut map = serializer.serialize_map(Some(4))?;
        map.serialize_entry("migration", &self.migration)?;
        map.serialize_entry("replaces", &self.replaces)?;
        map.serialize_entry(
            "snapshot_checksum",
            &encode_checksum(&self.snapshot_checksum),
        )?;
        map.serialize_entry("steps", &self.steps)?;
        map.end()
    }
}

/// Why an `ir.json` could not be read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SnapshotError {
    /// What is wrong and what to do about it.
    pub message: String,
}

impl std::fmt::Display for SnapshotError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for SnapshotError {}

/// The schema `ir_version` this ferro writes. Version 3 added the optional
/// envelope key `replaces`, a squash's witness (ADR-0056); version 2 added
/// the rename hints (`renamed_from` on a model, a column and a foreign key;
/// ADR-0032); version 1 is the same shape without either.
pub const CURRENT_SCHEMA_IR_VERSION: u32 = 3;

/// The key beside the envelope's own that links a snapshot to its parent.
const PARENT_CHECKSUM_KEY: &str = "parent_checksum";

/// The key beside the envelope's own that carries a squash's witness.
const REPLACES_KEY: &str = "replaces";

/// The first schema `ir_version` whose envelope may carry `replaces`.
const WITNESS_IR_VERSION: u32 = 3;

impl Snapshot {
    /// Load an `ir.json` written by any ferro version.
    ///
    /// Every schema `ir_version` ferro has shipped loads for good (ADR-0023):
    /// an older version goes through its own loader into today's IR types.
    /// An absent `parent_checksum` reads as no parent — the shape of a bare
    /// IR envelope, which the golden vectors pin; a migration's place in the
    /// chain is verified against its neighbour's checksum, never assumed.
    ///
    /// A squash's witness (`replaces`) is read beside `parent_checksum`, and
    /// only from a version 3 envelope or later; an absent one reads as none.
    ///
    /// # Errors
    /// A [`SnapshotError`] naming the problem when the bytes are not JSON, not
    /// a `schema` IR, of an `ir_version` newer than this ferro reads, or carry a
    /// malformed `parent_checksum` or witness, or a witness on an envelope
    /// older than version 3.
    pub fn load(bytes: &[u8]) -> Result<Snapshot, SnapshotError> {
        let fail = |message: String| SnapshotError { message };
        let mut document: serde_json::Value = serde_json::from_slice(bytes)
            .map_err(|err| fail(format!("is not valid JSON ({err})")))?;
        let object = document
            .as_object_mut()
            .ok_or_else(|| fail("is not a JSON object".to_string()))?;
        let parent_checksum = match object.remove(PARENT_CHECKSUM_KEY) {
            None | Some(serde_json::Value::Null) => None,
            Some(serde_json::Value::String(hex)) => {
                Some(decode_checksum(&hex).ok_or_else(|| {
                    fail(format!(
                        "has parent_checksum {hex:?}, which is not a SHA-384 in hex (96 characters)"
                    ))
                })?)
            }
            Some(other) => {
                return Err(fail(format!(
                    "has parent_checksum {other}, which is neither null nor a hex SHA-384"
                )));
            }
        };
        let witness = object.remove(REPLACES_KEY);
        let kind = object.get("ir_kind").and_then(|v| v.as_str()).unwrap_or("");
        if kind != "schema" {
            return Err(fail(format!(
                "has ir_kind '{kind}'; a schema snapshot is a 'schema' IR envelope"
            )));
        }
        let version = object
            .get("ir_version")
            .and_then(|v| v.as_u64())
            .ok_or_else(|| fail("has no numeric ir_version".to_string()))?;
        if version == 0 || version > u64::from(CURRENT_SCHEMA_IR_VERSION) {
            return Err(fail(format!(
                "has ir_version {version}, which this ferro cannot read (it reads schema \
                 ir_version 1 through {CURRENT_SCHEMA_IR_VERSION}); it was written by a \
                 newer ferro, so upgrade ferro"
            )));
        }
        let replaces = match witness {
            None => Vec::new(),
            Some(_) if version < u64::from(WITNESS_IR_VERSION) => {
                return Err(fail(format!(
                    "is a schema IR v{version} envelope carrying {REPLACES_KEY}, which only \
                     schema ir_version {WITNESS_IR_VERSION} records"
                )));
            }
            Some(value) => load_witness(&value).map_err(fail)?,
        };
        let ir = match version {
            1 => load_v1(document).map_err(fail)?,
            _ => {
                serde_json::from_value::<IrEnvelope<SchemaIrPayload>>(document).map_err(|err| {
                    fail(format!(
                        "is not a valid schema IR v{version} envelope ({err})"
                    ))
                })?
            }
        };
        Ok(Snapshot {
            ir,
            checksum: sha384(bytes),
            parent_checksum,
            replaces,
        })
    }

    /// The canonical bytes of the snapshot of `ir` whose parent is
    /// `parent_checksum` and whose witness is `replaces`: the envelope plus
    /// `parent_checksum` (lowercase hex, or `null` for the first migration)
    /// and, only when `replaces` is not empty, `replaces`; every object's
    /// keys sorted, two-space indented, ending in a newline. The same
    /// modelset always stores to the same bytes.
    ///
    /// # Errors
    /// A [`SnapshotError`] when the modelset does not serialize to a JSON
    /// object, or when a witness is given for an envelope older than version
    /// 3, which could not load it back: a snapshot that cannot be written is
    /// a refusal, never a placeholder.
    pub fn store(
        ir: &IrEnvelope<SchemaIrPayload>,
        parent_checksum: Option<[u8; 48]>,
        replaces: &[WitnessEntry],
    ) -> Result<Vec<u8>, SnapshotError> {
        let fail = |message: String| SnapshotError { message };
        if !replaces.is_empty() && ir.ir_version < WITNESS_IR_VERSION {
            return Err(fail(format!(
                "cannot carry {REPLACES_KEY} on a schema IR v{} envelope; only schema \
                 ir_version {WITNESS_IR_VERSION} records a witness",
                ir.ir_version
            )));
        }
        let mut document = serde_json::to_value(ir)
            .map_err(|err| fail(format!("cannot be serialized ({err})")))?;
        let object = document
            .as_object_mut()
            .ok_or_else(|| fail("does not serialize to a JSON object".to_string()))?;
        object.insert(
            PARENT_CHECKSUM_KEY.to_string(),
            parent_checksum
                .map(|checksum| serde_json::Value::String(encode_checksum(&checksum)))
                .unwrap_or(serde_json::Value::Null),
        );
        if !replaces.is_empty() {
            let witness = serde_json::to_value(replaces)
                .map_err(|err| fail(format!("cannot be serialized ({err})")))?;
            object.insert(REPLACES_KEY.to_string(), witness);
        }
        let mut bytes = serde_json::to_vec_pretty(&canonical(document))
            .map_err(|err| fail(format!("cannot be serialized ({err})")))?;
        bytes.push(b'\n');
        Ok(bytes)
    }
}

/// Schema IR v1: today's shape with no rename hint anywhere. A `renamed_from`
/// in a v1 document is not a v1 snapshot, so it is refused rather than read
/// as a hint no v1 ferro could have written.
fn load_v1(document: serde_json::Value) -> Result<IrEnvelope<SchemaIrPayload>, String> {
    let ir = serde_json::from_value::<IrEnvelope<SchemaIrPayload>>(document)
        .map_err(|err| format!("is not a valid schema IR v1 envelope ({err})"))?;
    let hinted = ir.payload.models.iter().find(|model| {
        model.renamed_from.is_some()
            || model.columns.iter().any(|col| col.renamed_from.is_some())
            || model
                .foreign_keys
                .iter()
                .any(|fk| fk.renamed_from.is_some())
    });
    if let Some(model) = hinted {
        return Err(format!(
            "is a schema IR v1 envelope whose model '{}' carries renamed_from, which only \
             schema ir_version 2 records",
            model.model_name
        ));
    }
    Ok(ir)
}

/// A squash's witness: a non-empty list of entries, each exactly
/// `{migration, snapshot_checksum, steps, replaces}`, naming every
/// migration once across the whole tree. Anything else is refused, never
/// read as a shorter witness: a witness read short would report an
/// adoptable database ahead.
fn load_witness(value: &serde_json::Value) -> Result<Vec<WitnessEntry>, String> {
    let mut seen = BTreeSet::new();
    match value.as_array() {
        Some(entries) if !entries.is_empty() => load_witness_entries(entries, &mut seen),
        _ => Err(format!(
            "has {REPLACES_KEY} {value}, which is not a non-empty list of replaced migrations"
        )),
    }
}

fn load_witness_entries(
    entries: &[serde_json::Value],
    seen: &mut BTreeSet<String>,
) -> Result<Vec<WitnessEntry>, String> {
    entries
        .iter()
        .map(|entry| load_witness_entry(entry, seen))
        .collect()
}

fn load_witness_entry(
    entry: &serde_json::Value,
    seen: &mut BTreeSet<String>,
) -> Result<WitnessEntry, String> {
    const KEYS: [&str; 4] = ["migration", "replaces", "snapshot_checksum", "steps"];
    let malformed = || {
        format!(
            "has a {REPLACES_KEY} entry {entry}, which is not an object of exactly \
             migration, snapshot_checksum, steps and replaces"
        )
    };
    let object = entry.as_object().ok_or_else(malformed)?;
    if object.len() != KEYS.len() || !KEYS.iter().all(|key| object.contains_key(*key)) {
        return Err(malformed());
    }
    let migration = object["migration"]
        .as_str()
        .filter(|name| is_migration_name(name))
        .ok_or_else(|| {
            format!(
                "has a {REPLACES_KEY} entry naming {}, which is not a migration directory \
                 name (NNNN_<name>)",
                object["migration"]
            )
        })?
        .to_string();
    let snapshot_checksum = object["snapshot_checksum"]
        .as_str()
        .and_then(decode_checksum)
        .ok_or_else(|| {
            format!(
                "has a {REPLACES_KEY} entry for {migration} whose snapshot_checksum {} is \
                 not a SHA-384 in hex (96 characters)",
                object["snapshot_checksum"]
            )
        })?;
    let steps = object["steps"]
        .as_array()
        .and_then(|ordinals| {
            ordinals
                .iter()
                .map(step_ordinal)
                .collect::<Option<Vec<u8>>>()
        })
        .filter(|steps| !steps.is_empty() && steps.windows(2).all(|pair| pair[0] < pair[1]))
        .ok_or_else(|| {
            format!(
                "has a {REPLACES_KEY} entry for {migration} whose steps {} are not a \
                 non-empty ascending list of step ordinals (1 to 99)",
                object["steps"]
            )
        })?;
    if !seen.insert(migration.clone()) {
        return Err(format!(
            "has {REPLACES_KEY} naming {migration} twice: a migration is replaced once, so \
             no entry, nested or not, repeats another"
        ));
    }
    let nested = object["replaces"].as_array().ok_or_else(|| {
        format!(
            "has a {REPLACES_KEY} entry for {migration} whose own {REPLACES_KEY} {} is not \
             a list",
            object["replaces"]
        )
    })?;
    let replaces = load_witness_entries(nested, seen)?;
    Ok(WitnessEntry {
        migration,
        snapshot_checksum,
        steps,
        replaces,
    })
}

/// The step ordinal `value` spells, `1` through `99`.
fn step_ordinal(value: &serde_json::Value) -> Option<u8> {
    value
        .as_u64()
        .and_then(|n| u8::try_from(n).ok())
        .filter(|n| (1..=99).contains(n))
}

/// Whether `name` spells a migration directory's name, `NNNN_<name>`, from
/// `0001`.
fn is_migration_name(name: &str) -> bool {
    let Some((number, rest)) = name.split_at_checked(4) else {
        return false;
    };
    let Some(rest) = rest.strip_prefix('_') else {
        return false;
    };
    number.chars().all(|c| c.is_ascii_digit())
        && number != "0000"
        && !rest.is_empty()
        && rest.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// `value` with every object's keys in sorted order, whatever map
/// implementation `serde_json` was built with.
fn canonical(value: serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::Object(map) => {
            let sorted: std::collections::BTreeMap<String, serde_json::Value> =
                map.into_iter().map(|(k, v)| (k, canonical(v))).collect();
            serde_json::Value::Object(sorted.into_iter().collect())
        }
        serde_json::Value::Array(items) => {
            serde_json::Value::Array(items.into_iter().map(canonical).collect())
        }
        other => other,
    }
}

/// SHA-384 of `bytes`.
pub fn sha384(bytes: &[u8]) -> [u8; 48] {
    use sha2::Digest;
    sha2::Sha384::digest(bytes).into()
}

/// A checksum as lowercase hex, the spelling `ir.json` and every report use.
pub fn encode_checksum(checksum: &[u8; 48]) -> String {
    checksum.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// The checksum `hex` spells, when it spells one.
pub fn decode_checksum(hex: &str) -> Option<[u8; 48]> {
    if hex.len() != 96 || !hex.is_ascii() {
        return None;
    }
    let mut out = [0u8; 48];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&hex[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn vectors_dir() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/ir_vectors")
    }

    /// Every golden schema vector, as the `ir` member of its vector file.
    fn schema_vectors() -> Vec<(String, Vec<u8>)> {
        let mut found = Vec::new();
        for entry in std::fs::read_dir(vectors_dir()).expect("ir_vectors directory") {
            let path = entry.expect("dir entry").path();
            let name = path.file_name().unwrap().to_string_lossy().to_string();
            if !name.starts_with("schema_") || !name.ends_with(".json") {
                continue;
            }
            let vector: serde_json::Value =
                serde_json::from_slice(&std::fs::read(&path).expect("read vector")).expect("json");
            let ir = serde_json::to_vec(&vector["ir"]).expect("serialize ir");
            found.push((name, ir));
        }
        found.sort();
        found
    }

    #[test]
    fn every_shipped_schema_vector_loads_through_the_snapshot_loader() {
        let vectors = schema_vectors();
        assert!(vectors.len() >= 3, "expected the golden schema vectors");
        for (name, bytes) in vectors {
            let snapshot = Snapshot::load(&bytes)
                .unwrap_or_else(|err| panic!("{name} must load: {}", err.message));
            assert_eq!(snapshot.ir.ir_kind, "schema", "{name}");
            assert!(!snapshot.ir.payload.models.is_empty(), "{name}");
            assert_eq!(snapshot.checksum, sha384(&bytes), "{name}");
            if name != WITNESS_VECTOR {
                assert_eq!(snapshot.parent_checksum, None, "{name}");
                assert_eq!(snapshot.replaces, [], "{name}: no witness before v3");
            }
        }
    }

    #[test]
    fn storing_the_same_modelset_twice_yields_identical_bytes_that_load_back() {
        for (name, bytes) in schema_vectors() {
            let loaded = Snapshot::load(&bytes).expect("load");
            let parent = Some(sha384(b"parent"));
            let first = Snapshot::store(&loaded.ir, parent, &loaded.replaces).expect("store");
            let second = Snapshot::store(&loaded.ir, parent, &loaded.replaces).expect("store");
            assert_eq!(first, second, "{name}: canonical bytes are deterministic");
            assert!(first.ends_with(b"\n"), "{name}: ends with a newline");
            let back = Snapshot::load(&first).expect("stored snapshot loads");
            assert_eq!(back.ir, loaded.ir, "{name}: the modelset round-trips");
            assert_eq!(back.parent_checksum, parent, "{name}");
            assert_eq!(
                back.replaces, loaded.replaces,
                "{name}: the witness round-trips"
            );
            assert_eq!(back.checksum, sha384(&first), "{name}");
        }
    }

    #[test]
    fn stored_json_has_sorted_keys_and_a_hex_parent_checksum() {
        let (_, bytes) = schema_vectors().remove(0);
        let ir = Snapshot::load(&bytes).expect("load").ir;
        let root = Snapshot::store(&ir, None, &[]).expect("store");
        let text = String::from_utf8(root).expect("utf-8");
        assert!(text.contains("\"parent_checksum\": null"), "{text}");
        let keys: Vec<&str> = text
            .lines()
            .filter(|line| line.starts_with("  \"") && !line.starts_with("    "))
            .map(|line| line.trim().split('"').nth(1).unwrap())
            .collect();
        assert_eq!(
            keys,
            ["ir_kind", "ir_version", "parent_checksum", "payload"]
        );

        let child = String::from_utf8(Snapshot::store(&ir, Some([0xab; 48]), &[]).expect("store"))
            .expect("utf-8");
        assert!(child.contains(&format!("\"parent_checksum\": \"{}\"", "ab".repeat(48))));
    }

    #[test]
    fn a_v1_snapshot_and_a_v2_snapshot_with_rename_hints_both_load() {
        let version = |bytes: &[u8]| {
            serde_json::from_slice::<serde_json::Value>(bytes).expect("json")["ir_version"]
                .as_u64()
                .expect("numeric ir_version")
        };
        let vectors = schema_vectors();
        assert!(vectors.iter().any(|(_, b)| version(b) == 1), "a v1 vector");
        let (name, bytes) = vectors
            .iter()
            .find(|(_, b)| version(b) == 2)
            .expect("a v2 vector is pinned");
        let ir = Snapshot::load(bytes)
            .unwrap_or_else(|err| panic!("{name} must load: {}", err.message))
            .ir;
        assert_eq!(u64::from(CURRENT_SCHEMA_IR_VERSION), 3);
        let models = &ir.payload.models;
        assert!(
            models.iter().any(|m| m.renamed_from.is_some()),
            "table hint"
        );
        assert!(
            models
                .iter()
                .flat_map(|m| &m.columns)
                .any(|c| c.renamed_from.is_some()),
            "column hint"
        );
        assert!(
            models
                .iter()
                .flat_map(|m| &m.foreign_keys)
                .any(|fk| fk.renamed_from.is_some()),
            "foreign-key hint"
        );
    }

    #[test]
    fn a_v1_snapshot_carrying_a_rename_hint_is_refused() {
        let v1 = br#"{"ir_kind": "schema", "ir_version": 1, "payload": {"dialect_agnostic": true, "models": [{"model_name": "Author", "table_name": "author", "renamed_from": "writer", "columns": [], "foreign_keys": [], "indexes": [], "uniques": [], "checks": []}]}}"#;
        let err = Snapshot::load(v1).expect_err("v1 cannot carry hints");
        assert!(err.message.contains("renamed_from"), "{}", err.message);
    }

    #[test]
    fn a_snapshot_from_a_newer_ferro_or_of_another_kind_is_refused() {
        let newer = br#"{"ir_kind": "schema", "ir_version": 999, "parent_checksum": null, "payload": {"dialect_agnostic": true, "models": []}}"#;
        let err = Snapshot::load(newer).expect_err("newer ir_version");
        assert!(err.message.contains("ir_version 999"), "{}", err.message);
        assert!(err.message.contains("upgrade ferro"), "{}", err.message);

        let query = br#"{"ir_kind": "query", "ir_version": 1, "payload": {}}"#;
        let err = Snapshot::load(query).expect_err("query kind");
        assert!(err.message.contains("'query'"), "{}", err.message);

        let err = Snapshot::load(b"{not json").expect_err("malformed");
        assert!(err.message.contains("not valid JSON"), "{}", err.message);

        let bad_parent = br#"{"ir_kind": "schema", "ir_version": 1, "parent_checksum": "abc", "payload": {"dialect_agnostic": true, "models": []}}"#;
        let err = Snapshot::load(bad_parent).expect_err("bad checksum");
        assert!(err.message.contains("parent_checksum"), "{}", err.message);
    }

    /// The golden vector of a squash whose witness nests a squash.
    const WITNESS_VECTOR: &str = "schema_squash_witness_v3.json";

    fn witness_vector() -> serde_json::Value {
        let path = vectors_dir().join(WITNESS_VECTOR);
        let vector: serde_json::Value =
            serde_json::from_slice(&std::fs::read(path).expect("read vector")).expect("json");
        vector["ir"].clone()
    }

    /// `ir` as the canonical bytes a stored snapshot has.
    fn canonical_bytes(ir: &serde_json::Value) -> Vec<u8> {
        let mut bytes = serde_json::to_vec_pretty(&canonical(ir.clone())).expect("serialize");
        bytes.push(b'\n');
        bytes
    }

    /// The v3 vector with `edit` applied to its first witness entry.
    fn edited_witness(edit: impl FnOnce(&mut serde_json::Value)) -> Vec<u8> {
        let mut ir = witness_vector();
        edit(&mut ir["replaces"][0]);
        canonical_bytes(&ir)
    }

    #[test]
    fn the_v3_vector_reads_its_nested_witness_and_stores_back_byte_for_byte() {
        let bytes = canonical_bytes(&witness_vector());
        let snapshot = Snapshot::load(&bytes).expect("the v3 vector loads");
        assert_eq!(snapshot.ir.ir_version, 3);
        let hash = |s: &str| sha384(s.as_bytes());
        let entry = |migration: &str, steps: &[u8], replaces: Vec<WitnessEntry>| WitnessEntry {
            migration: migration.to_string(),
            snapshot_checksum: hash(&format!("{migration}/ir.json")),
            steps: steps.to_vec(),
            replaces,
        };
        assert_eq!(
            snapshot.replaces,
            [
                entry(
                    "0005_teams_epic",
                    &[1, 2],
                    vec![
                        entry("0005_add_teams", &[1], vec![]),
                        entry("0006_team_slug", &[1, 2], vec![]),
                    ],
                ),
                entry("0006_drop_tmp", &[1], vec![]),
            ]
        );
        let stored = Snapshot::store(&snapshot.ir, snapshot.parent_checksum, &snapshot.replaces)
            .expect("store");
        assert_eq!(
            String::from_utf8(stored).expect("utf-8"),
            String::from_utf8(bytes).expect("utf-8")
        );
    }

    #[test]
    fn a_v1_or_v2_envelope_carrying_replaces_is_refused() {
        for version in [1, 2] {
            let mut ir = witness_vector();
            ir["ir_version"] = serde_json::json!(version);
            let err = Snapshot::load(&canonical_bytes(&ir)).expect_err("replaces is v3's");
            assert!(
                err.message.contains(&format!("v{version}")) && err.message.contains("replaces"),
                "{}",
                err.message
            );
        }
    }

    #[test]
    fn a_v4_envelope_keeps_the_upgrade_refusal() {
        let mut ir = witness_vector();
        ir["ir_version"] = serde_json::json!(4);
        let err = Snapshot::load(&canonical_bytes(&ir)).expect_err("newer");
        assert!(err.message.contains("ir_version 4"), "{}", err.message);
        assert!(err.message.contains("upgrade ferro"), "{}", err.message);
    }

    #[test]
    fn a_malformed_witness_is_refused_never_read_as_nothing() {
        let cases: Vec<(&str, Vec<u8>, &str)> = vec![
            (
                "95-character checksum",
                edited_witness(|entry| {
                    let hex = entry["snapshot_checksum"]
                        .as_str()
                        .expect("hex")
                        .to_string();
                    entry["snapshot_checksum"] = serde_json::json!(hex[..95]);
                }),
                "snapshot_checksum",
            ),
            (
                "no steps",
                edited_witness(|entry| entry["steps"] = serde_json::json!([])),
                "steps",
            ),
            (
                "a nested repeat",
                edited_witness(|entry| {
                    entry["replaces"][1]["migration"] = serde_json::json!("0006_drop_tmp");
                }),
                "0006_drop_tmp",
            ),
            (
                "a step that is no ordinal",
                edited_witness(|entry| entry["steps"] = serde_json::json!([0])),
                "steps",
            ),
            (
                "a name that is no migration",
                edited_witness(|entry| entry["migration"] = serde_json::json!("teams")),
                "teams",
            ),
            (
                "an unknown key",
                edited_witness(|entry| entry["why"] = serde_json::json!("x")),
                "why",
            ),
            (
                "an empty witness",
                canonical_bytes(&{
                    let mut ir = witness_vector();
                    ir["replaces"] = serde_json::json!([]);
                    ir
                }),
                "replaces",
            ),
        ];
        for (case, bytes, named) in cases {
            let err = Snapshot::load(&bytes).expect_err(case);
            assert!(err.message.contains("replaces"), "{case}: {}", err.message);
            assert!(err.message.contains(named), "{case}: {}", err.message);
        }
    }

    #[test]
    fn a_witness_is_never_stored_on_an_envelope_older_than_v3() {
        let snapshot = Snapshot::load(&canonical_bytes(&witness_vector())).expect("load");
        let mut v2 = snapshot.ir.clone();
        v2.ir_version = 2;
        let err = Snapshot::store(&v2, None, &snapshot.replaces).expect_err("v2 has no witness");
        assert!(err.message.contains("replaces"), "{}", err.message);
    }
}
